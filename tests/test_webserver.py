import asyncio
import socket
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def handshake(port, timeout=3.0):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            raw.settimeout(timeout)
            with ctx.wrap_socket(raw, server_hostname="x"):
                return True
    except OSError:
        return False


class WatchdogTests(unittest.TestCase):
    def test_reopens_a_server_that_stopped_accepting(self):
        port = free_port()
        srv = server.HorizonServer(None, ("127.0.0.1", 8989))
        loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(srv.run(host="127.0.0.1", port=port))
            started.set()
            loop.run_forever()
        with mock.patch.object(server.HorizonServer, "WATCHDOG_SECONDS", 0.3), \
                mock.patch.object(server.HorizonServer, "WATCHDOG_TIMEOUT", 0.5), \
                mock.patch.object(server, "server_log", lambda line: None):
            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(started.wait(10))
            try:
                self.assertTrue(handshake(port))
                asyncio.run_coroutine_threadsafe(self._pause(srv), loop).result(5)
                self.assertFalse(handshake(port, timeout=0.5))
                deadline = time.monotonic() + 15
                ok = False
                while time.monotonic() < deadline and not ok:
                    ok = handshake(port, timeout=1.0)
                self.assertTrue(ok, "watchdog didn't bring the server back")
            finally:
                srv._stopped.set()
                loop.call_soon_threadsafe(loop.stop)

    @staticmethod
    async def _pause(srv):
        for s in srv._site._server.sockets:
            asyncio.get_running_loop().remove_reader(s.fileno())


if __name__ == "__main__":
    unittest.main()
