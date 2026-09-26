import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from testlib import web

BODY = bytes(range(256)) * 20000  # 5,120,000 bytes


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def do_PUT(self):
        length = self.headers.get("Content-Length")
        encoding = self.headers.get("Transfer-Encoding", "")
        if length is not None:
            got = self.rfile.read(int(length))
        else:
            got = b""
            while True:
                size = int(self.rfile.readline().strip(), 16)
                piece = self.rfile.read(size + 2)[:size]
                if not size:
                    break
                got += piece
        reply = f"{length} {encoding} {len(got)} {got == BODY[:len(got)]}".encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


class Request(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/x"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_bytes(self):
        self.assertEqual(web.request("PUT", self.url, upload=BODY[:7]).body, b"7  7 True")
        self.assertEqual(web.request("PUT", self.url, upload=BODY[:7], chunked=True).body,
                         b"None chunked 7 True")

    def test_chunked_in_pieces(self):
        answer = web.request("PUT", self.url, upload=BODY[:200000], chunked=True)
        self.assertEqual(answer.body, b"None chunked 200000 True")

    def test_get(self):
        answer = web.request("GET", self.url)
        self.assertEqual((answer.status, answer.body), (200, BODY))
