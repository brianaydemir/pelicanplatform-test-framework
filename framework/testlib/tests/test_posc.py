import socket
import ssl
import threading
import unittest
from unittest import mock

from suites import posc


class PartialPut(unittest.TestCase):
    """An upload that never got under way must not pass for one cut
    short: `overwrite` would find the original intact."""

    def test_refused_connection(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(posc.CouldNotConnect):
            posc.partial_put(f"https://127.0.0.1:{port}/x", "sized", 2, 1, 0, "t")

    def test_failed_handshake(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def hang_up() -> None:
            conn, _ = server.accept()
            conn.close()

        helper = threading.Thread(target=hang_up)
        helper.start()
        url = f"https://127.0.0.1:{server.getsockname()[1]}/x"
        try:
            with mock.patch.object(posc.web, "tls_context", ssl.create_default_context):
                with self.assertRaises(posc.CouldNotConnect):
                    posc.partial_put(url, "sized", 2, 1, 0, "t")
        finally:
            helper.join()
            server.close()

    def test_wrong_answer(self):
        self.assertIsNotNone(posc.wrong_answer("201"))
        self.assertIsNotNone(posc.wrong_answer("405"))


if __name__ == "__main__":
    unittest.main()
