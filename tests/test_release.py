import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import core, recording, server, streaming


class ServerStartTests(unittest.TestCase):
    def test_a_taken_port_is_reported_not_hidden(self):
        hold = socket.socket()
        hold.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        hold.bind(("0.0.0.0", 0))
        hold.listen(1)
        port = hold.getsockname()[1]
        orch = core.Orchestrator()
        try:
            with mock.patch.object(core.config, "WEB_PORT", port), \
                    mock.patch.object(server, "server_log", lambda line: None), \
                    mock.patch.object(orch.switcher, "start"), mock.patch.object(orch.air, "start"), \
                    mock.patch.object(orch.anim, "start"):
                status = orch.start(start_capture=False)
            self.assertIn(str(port), status["server_error"] or "")
            self.assertIsNone(orch.server)
        finally:
            hold.close()
            with mock.patch.object(orch.air, "stop"), mock.patch.object(orch.anim, "stop"):
                orch.stop()


class MediaServerTests(unittest.TestCase):
    def test_a_crashed_media_server_is_started_again(self):
        sc = streaming.StreamController()

        class Dead:
            returncode = 3

            def poll(self):
                return 3
        sc._mtx = Dead()
        starts = []
        sc.start_mediamtx = lambda: starts.append(1) or True
        sc._stop_capture = lambda: None
        self.assertFalse(sc._keep_mediamtx())
        self.assertEqual(starts, [1])
        self.assertIn("media server stopped", sc.last_error)
        self.assertFalse(sc._keep_mediamtx())
        self.assertEqual(starts, [1])
        sc._mtx_restarted = time.monotonic() - 10
        sc._keep_mediamtx()
        self.assertEqual(starts, [1, 1])


class PhoneInputTests(unittest.TestCase):
    def setUp(self):
        self.srv = server.HorizonServer(None, ("127.0.0.1", 8989))

    def test_odd_values_never_raise(self):
        for bad in ({"gap_track_smooth": ["x"]}, {"zoom": "big"}, {"zoom": float("nan")},
                    {"euler": "up"}, {"position": [1, "a", None]}, {"control_sequence": 3, "control_id": "x"},
                    {"gap_track_range": {}}, {"fov_base": None}):
            self.srv._ingest(bad)
            s = self.srv._sample
            self.assertTrue(s["zoom"] is None or s["zoom"] == s["zoom"])
            self.assertIn(s["gap_track_smooth"], ("low", "medium", "high"))


class RecordingAudioTests(unittest.TestCase):
    def test_a_missing_sound_device_ends_the_audio_cleanly(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        audio = recording._LoopbackAudio.__new__(recording._LoopbackAudio)
        audio.rate, audio.channels = 48000, 2
        audio._device = {"index": 0}

        class PA:
            def open(self, **kw):
                raise OSError("device unplugged")
        audio._pa = PA()
        import threading
        stop = threading.Event()
        fake_pyaudio = mock.Mock(paInt16=8)
        with mock.patch.dict(sys.modules, {"pyaudiowpatch": fake_pyaudio}):
            t = threading.Thread(target=audio.pump, args=(port, stop))
            t.start()
            conn, _ = listener.accept()
            conn.settimeout(3)
            self.assertEqual(conn.recv(10), b"")
            t.join(3)
        self.assertFalse(t.is_alive())
        conn.close()
        listener.close()


if __name__ == "__main__":
    unittest.main()
