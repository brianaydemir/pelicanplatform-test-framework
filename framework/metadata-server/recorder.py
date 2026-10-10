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
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, cast

UPSTREAM = os.environ.get("RECORDER_UPSTREAM", "http://metadata-verifier:9999/events")
DIR = os.environ.get("RECORDER_DIR", "/captures")
PORT = int(os.environ.get("RECORDER_PORT", "9999"))

# Passed through unchanged; the verifier needs all of them.
FORWARDED = ("Content-Type", "Authorization", "X-Pelican-Idempotency-Key", "User-Agent")

lock = threading.Lock()
failed_once: set[str] = set()


def parse_multipart(content_type: str, body: bytes) -> list[tuple[dict[str, str], bytes]]:
    """Return the parts of a multipart body as (headers, payload) pairs."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("multipart body without a boundary")
    delimiter = b"--" + m.group(1).encode()
    parts: list[tuple[dict[str, str], bytes]] = []
    # A preamble comes before the first delimiter, "--" after the last.
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        headers: dict[str, str] = {}
        for line in head.decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        parts.append((headers, payload))
    return parts


def split_event(content_type: str, body: bytes) -> tuple[bytes, Optional[bytes]]:
    """Return the event's JSON bytes and the blob (or None)."""
    if not content_type.lower().startswith("multipart/"):
        return body, None
    event: Optional[bytes] = None
    blob: Optional[bytes] = None
    for headers, payload in parse_multipart(content_type, body):
        if event is None and "json" in headers.get("content-type", ""):
            event = payload
        else:
            blob = payload
    if event is None:
        raise ValueError("multipart body without a JSON part")
    return event, blob


def field(event: Any, *keys: str) -> str:
    """The event's value at keys (e.g. "object", "path") as a string, or
    "-" if it has none."""
    value: Any = event
    for key in keys:
        if not isinstance(value, dict):
            return "-"
        mapping = cast(dict[str, Any], value)
        if key not in mapping:
            return "-"
        value = mapping[key]
    return str(value)


class Handler(BaseHTTPRequestHandler):
    """Records each POST, and answers health checks."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        """Log nothing: do_POST prints a line per event instead."""

    def reply(self, status: int, data: bytes) -> None:
        """Answer with status and data, as plain text."""
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> bytes:
        """The request's body, whether chunked or of a declared length."""
        if "chunked" not in self.headers.get("Transfer-Encoding", "").lower():
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = b""
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return data
            data += self.rfile.read(size)
            self.rfile.readline()

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Answer /healthz with 200 if the upstream's /healthz does."""
        if self.path != "/healthz":
            self.reply(404, b"not found\n")
            return
        scheme, _, host = UPSTREAM.split("/", 3)[:3]
        try:
            with urllib.request.urlopen(f"{scheme}//{host}/healthz", timeout=5):  # nosec B310
                self.reply(200, b"ok\n")
        except (urllib.error.URLError, OSError) as err:
            self.reply(503, f"verifier: {err}\n".encode())

    def forward(self, body: bytes) -> tuple[int, bytes]:
        """Pass the request to the upstream: its status and answer."""
        headers = {h: self.headers[h] for h in FORWARDED if self.headers.get(h)}
        request = urllib.request.Request(UPSTREAM, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:  # nosec B310
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()
        except (urllib.error.URLError, OSError) as err:
            return 502, f"verifier unreachable: {err}\n".encode()

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """Save the event, then fail it on purpose or pass it on."""
        body = self.read_body()
        rid = str(time.time_ns())
        record: dict[str, Any] = {
            "received": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": self.path,
            "headers": dict(self.headers.items()),
        }

        event: Any = None
        try:
            event_bytes, blob = split_event(self.headers.get("Content-Type", ""), body)
            event = json.loads(event_bytes)
            with open(os.path.join(DIR, rid + ".event.json"), "wb") as f:
                f.write(event_bytes)
            if blob is not None:
                with open(os.path.join(DIR, rid + ".blob"), "wb") as f:
                    f.write(blob)
        except ValueError as err:
            record["parse_error"] = str(err)
        etype, opath, eid = (
            field(event, "type"),
            field(event, "object", "path"),
            field(event, "id"),
        )

        name = opath.rsplit("/", 1)[-1]
        fault: Optional[str] = None
        with lock:
            fail_once = name.startswith("fail-once-") and eid not in failed_once
            if fail_once:
                failed_once.add(eid)
        if name.startswith("reject-"):
            fault, status, answer = "reject", 422, b"rejected by the recorder\n"
        elif fail_once:
            fault, status, answer = "fail-once", 503, b"failed once by the recorder\n"
        else:
            status, answer = self.forward(body)

        answer_text = answer.decode("utf-8", "replace")
        record.update(fault=fault, status=status, response=answer_text)
        line = "\t".join([rid, str(status), etype, opath, eid])
        with lock:
            with open(os.path.join(DIR, rid + ".json"), "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
            with open(os.path.join(DIR, "index.tsv"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        print(line, answer_text.strip(), sep="\t", flush=True)
        self.reply(status, answer)


def main() -> None:
    """Serve until killed."""
    os.makedirs(DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # nosec B104
    print(f"recording to {DIR}, passing to {UPSTREAM}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
