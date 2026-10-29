import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import relay, streaming


class ResizeRestartTests(unittest.TestCase):
    def make(self):
        sc = streaming.StreamController()
        starts = []
        sc.capture_alive = lambda: bool(starts)
        sc._start_window = lambda window: (starts.append(window[1:]), setattr(sc, "mode", sc.WINDOW))
        sc._start_direct = lambda window: starts.append(window[1:])
        sc._stop_capture = lambda: None
        return sc, starts

    def test_restarts_when_the_game_resolution_changes(self):
        sc, starts = self.make()
        size = {"v": (1920, 1080)}
        with mock.patch.object(streaming.game, "game_pid", return_value=42), \
                mock.patch.object(streaming.game, "main_window", side_effect=lambda pid: (1, *size["v"])):
            sc._tick()
            sc._tick()
            self.assertEqual(starts, [(1920, 1080)])
            size["v"] = (3440, 1440)
            sc._tick()
            self.assertEqual(len(starts), 1)
            sc._tick()
            self.assertEqual(starts[-1], (3440, 1440))
            sc._tick()
            self.assertEqual(len(starts), 2)

    def test_a_passing_size_during_a_drag_doesnt_restart(self):
        sc, starts = self.make()
        sizes = iter([(1920, 1080), (1700, 1000), (1920, 1080), (1920, 1080)])
        with mock.patch.object(streaming.game, "game_pid", return_value=42), \
                mock.patch.object(streaming.game, "main_window", side_effect=lambda pid: (1, *next(sizes))):
            for _ in range(4):
                sc._tick()
        self.assertEqual(starts, [(1920, 1080)])


class QualityTests(unittest.TestCase):
    def make(self, recording):
        sc = streaming.StreamController(hold_restarts=lambda: recording["on"])
        starts = []
        sc.capture_alive = lambda: bool(starts)
        sc._start_window = lambda window: (starts.append(sc.bitrate), setattr(sc, "mode", sc.WINDOW))
        sc._stop_capture = lambda: None
        return sc, starts

    def test_live_view_stays_light_when_recordings_have_their_own_capture(self):
        recording = {"on": False}
        sc, starts = self.make(recording)
        with mock.patch.object(streaming, "direct_helper", return_value=Path("helper.exe")), \
                mock.patch.object(streaming.game, "game_pid", return_value=42), \
                mock.patch.object(streaming.game, "main_window", return_value=(1, 2560, 1440)):
            sc._tick()
            sc.set_quality("max")
            sc._tick()
            self.assertEqual(starts, ["12M"])

    def test_without_that_capture_the_live_stream_carries_the_quality(self):
        recording = {"on": False}
        sc, starts = self.make(recording)
        with mock.patch.object(streaming, "direct_helper", return_value=None), \
                mock.patch.object(streaming.game, "game_pid", return_value=42), \
                mock.patch.object(streaming.game, "main_window", return_value=(1, 2560, 1440)):
            sc._tick()
            recording["on"] = True
            sc.set_quality("max")
            sc._tick()
            self.assertEqual(starts, ["12M"])
            recording["on"] = False
            sc._tick()
            self.assertEqual(starts, ["12M", "50M"])
            sc.set_quality("nonsense")
            sc._tick()
            self.assertEqual(starts, ["12M", "50M"])

    def test_a_failed_recording_capture_falls_back_to_the_live_stream(self):
        sc = streaming.StreamController(direct_ready=lambda: True)
        sc.set_quality("high")
        with mock.patch.object(streaming, "direct_helper", return_value=Path("helper.exe")):
            self.assertEqual(sc.bitrate, "12M")
            sc.dedicated_failed = True
            self.assertEqual((sc.bitrate_bps, sc.nvenc_preset), (30_000_000, "p3"))
            self.assertIsNone(sc.recording_source())


class RecordingCaptureTests(unittest.TestCase):
    def test_record_sizes(self):
        from horizonlens import recording
        self.assertEqual(recording.record_size(608, 1080, "high"), (1080, 1920))
        self.assertEqual(recording.record_size(810, 1440, "high"), (1080, 1920))
        self.assertEqual(recording.record_size(810, 1440, "max"), (1080, 1920))
        self.assertEqual(recording.record_size(1214, 2160, "max"), (1214, 2160))
        self.assertEqual(recording.record_size(2560, 1440, "max"), (2560, 1440))
        self.assertEqual(recording.record_size(3840, 2160, "high"), (1920, 1080))

    def test_helper_command_and_fallback(self):
        from horizonlens import recording
        rec = recording.Recorder()
        rec.set_quality("max")
        failed = []
        rec.on_dedicated_failed = lambda: failed.append(1)
        rec.source = lambda: {"helper": "h.exe", "pid": 7, "client": (3440, 1440), "aspect": (9, 16), "fps": 60}
        seen = {}

        class FakeHelper:
            def __init__(self, cmd, **kw):
                seen["cmd"] = cmd
                self.stdin, self.stdout = mock.Mock(), mock.Mock()
                self.stderr = [b"REALCAM_FORZA_NVENC_ERROR no texture\n"]

            def poll(self):
                return 3

            def kill(self):
                pass
        with mock.patch.object(recording.subprocess, "Popen", FakeHelper), \
                mock.patch.object(recording.config, "recordings_dir", return_value=Path(__file__).parent):
            self.assertIsNone(rec._open_dedicated())
        cmd = seen["cmd"]
        self.assertEqual(cmd[:7], ["h.exe", "3440", "1440", "0", "0", "60", "50000000"])
        self.assertIn("--secondary", cmd)
        self.assertEqual(cmd[cmd.index("--output-size") + 1], "1080x1920")
        self.assertEqual(cmd[cmd.index("--fit-aspect") + 1], "9:16")
        (Path(__file__).parent / ".last-recording-capture.log").unlink(missing_ok=True)


class WalkingTests(unittest.TestCase):
    def walk(self, mps, seconds=6.0):
        fx = relay.WalkingFx()
        fx.settings = (True, 1.0, 1.0, 1.0)
        t, ys, out = 0.0, [], []
        for _ in range(int(seconds * 90)):
            t += 1 / 90
            o = fx.update((0.0, 0.0, mps), t)
            ys.append(o[0][1])
            out.append(o)
        return ys[-int(4 * 90):], out

    def steps_per_second(self, ys):
        lows = sum(1 for i in range(1, len(ys) - 1) if ys[i] < ys[i - 1] and ys[i] <= ys[i + 1] and ys[i] < 0)
        return lows / (len(ys) / 90)

    def test_cadence_is_human(self):
        stroll = self.steps_per_second(self.walk(1.2)[0])
        brisk = self.steps_per_second(self.walk(3.8)[0])
        run = self.steps_per_second(self.walk(9.6)[0])
        self.assertTrue(1.6 <= stroll <= 2.2, stroll)
        self.assertTrue(2.4 <= brisk <= 3.1, brisk)
        self.assertTrue(2.7 <= run <= 3.1, run)

    def test_small_smooth_bob(self):
        ys, out = self.walk(3.8)
        self.assertLess(max(ys) - min(ys), 0.045)
        for a, b in zip(out, out[1:]):
            self.assertLess(abs(a[0][1] - b[0][1]), 0.004)
            self.assertLess(max(abs(x - y) for x, y in zip(a[1], b[1])), 0.12)

    def test_stops_gently(self):
        fx = relay.WalkingFx()
        fx.settings = (True, 1.0, 1.0, 1.0)
        t = 0.0
        for _ in range(3 * 90):
            t += 1 / 90
            fx.update((0.0, 0.0, 3.0), t)
        prev, worst = fx.update((0.0, 0.0, 3.0), t + 1 / 90), 0.0
        t += 1 / 90
        for _ in range(3 * 90):
            t += 1 / 90
            o = fx.update((0.0, 0.0, 0.0), t)
            worst = max(worst, abs(o[0][1] - prev[0][1]))
            prev = o
        self.assertLess(worst, 0.0025)
        self.assertLess(abs(prev[0][1]), 0.001)


if __name__ == "__main__":
    unittest.main()


class AttachWaitTests(unittest.TestCase):

    def test_waits_for_a_driving_view(self):
        from horizonlens import camera_scan, carpose, companion

        class Tracker:
            pose_value = None

            def pose(self):
                return Tracker.pose_value

            def close(self):
                pass
        with mock.patch.object(camera_scan, "still_ready", return_value="DriverCam not registered (12 cameras)"):
            self.assertIn("DriverCam", companion.not_driving_yet(1, 2))
        with mock.patch.object(camera_scan, "still_ready", return_value=None), \
                mock.patch.object(carpose, "CarTracker", Tracker):
            self.assertEqual(companion.not_driving_yet(1, 2), "no drivable car yet")
            Tracker.pose_value = object()
            self.assertIsNone(companion.not_driving_yet(1, 2))

    def test_waiting_is_retried(self):
        from horizonlens import desktop
        msg = "Waiting for a driving view (not in a menu, loading screen or garage) before taking the camera."
        self.assertFalse(any(f in msg for f in desktop.MainWindow.AUTO_FINAL))
