"""Record the origins' metadata events, then pass them to the verifier.

Every POST is saved in RECORDER_DIR, named by the time it arrived:

  <id>.json        request headers, fault, and the status and body returned
  <id>.event.json  the event as sent
  <id>.blob        the uploader's opaque blob, if one came along

and summarized in index.tsv as `<id> <status> <type> <path> <event id>`
(tab-separated), written last. Faults, chosen by the basename of the
event's object path:

  reject-*     422, which the origin never retries
  fail-once-*  503 for the first delivery of each event, then as usual

Anything else goes to RECORDER_UPSTREAM, and its answer comes back.
GET /healthz answers 200 only if the upstream's /healthz does.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("RECORDER_UPSTREAM", "http://metadata-verifier:9999/events")
DIR = os.environ.get("RECORDER_DIR", "/captures")
PORT = int(os.environ.get("RECORDER_PORT", "9999"))

# Passed through unchanged; the verifier needs all of them.
FORWARDED = ("Content-Type", "Authorization", "X-Pelican-Idempotency-Key", "User-Agent")

lock = threading.Lock()
failed_once = set()


def parse_multipart(content_type, body):
    """Return the parts of a multipart body as (headers, payload) pairs."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("multipart body without a boundary")
    delimiter = b"--" + m.group(1).encode()
    parts = []
    # A preamble comes before the first delimiter, "--" after the last.
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers = {}
        for line in head.decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        parts.append((headers, payload))
    return parts


def split_event(content_type, body):
    """Return the event's JSON bytes and the blob (or None)."""
    if not content_type.lower().startswith("multipart/"):
        return body, None
    event, blob = None, None
    for headers, payload in parse_multipart(content_type, body):
        if event is None and "json" in headers.get("content-type", ""):
            event = payload
        else:
            blob = payload
    if event is None:
        raise ValueError("multipart body without a JSON part")
    return event, blob


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def reply(self, status, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        if self.path != "/healthz":
            self.reply(404, "not found\n")
            return
        health = UPSTREAM.split("/", 3)
        try:
            with urllib.request.urlopen("/".join(health[:3]) + "/healthz", timeout=5):
                self.reply(200, "ok\n")
        except (urllib.error.URLError, OSError) as err:
            self.reply(503, "verifier: %s\n" % err)

    def do_POST(self):
        body = self.read_body()
        content_type = self.headers.get("Content-Type", "")
        rid = str(time.time_ns())
        record = {
            "received": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": self.path,
            "headers": dict(self.headers.items()),
        }

        etype, opath, eid, fault = "-", "-", "-", None
        try:
            event_bytes, blob = split_event(content_type, body)
            event = json.loads(event_bytes)
            etype = event.get("type", "-")
            eid = event.get("id", "-")
            opath = str(event.get("object", {}).get("path", "-"))
            with open(os.path.join(DIR, rid + ".event.json"), "wb") as f:
                f.write(event_bytes)
            if blob is not None:
                with open(os.path.join(DIR, rid + ".blob"), "wb") as f:
                    f.write(blob)
        except (ValueError, AttributeError) as err:
            record["parse_error"] = str(err)

        name = opath.rsplit("/", 1)[-1]
        if name.startswith("reject-"):
            fault, status, answer = "reject", 422, "rejected by the recorder\n"
        elif name.startswith("fail-once-") and eid not in failed_once:
            with lock:
                failed_once.add(eid)
            fault, status, answer = "fail-once", 503, "failed once by the recorder\n"
        else:
            headers = {h: self.headers[h] for h in FORWARDED if self.headers.get(h)}
            request = urllib.request.Request(UPSTREAM, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    status, answer = resp.status, resp.read()
            except urllib.error.HTTPError as err:
                status, answer = err.code, err.read()
            except (urllib.error.URLError, OSError) as err:
                status, answer = 502, "verifier unreachable: %s\n" % err

        answer_text = answer.decode("utf-8", "replace") if isinstance(answer, bytes) else answer
        record.update(fault=fault, status=status, response=answer_text)
        line = "\t".join([rid, str(status), etype, opath, eid])
        with lock:
            with open(os.path.join(DIR, rid + ".json"), "w") as f:
                json.dump(record, f, indent=2)
            with open(os.path.join(DIR, "index.tsv"), "a") as f:
                f.write(line + "\n")
        print(line, answer_text.strip(), sep="\t", flush=True)
        self.reply(status, answer)


def main():
    os.makedirs(DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("recording to %s, passing to %s" % (DIR, UPSTREAM), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
