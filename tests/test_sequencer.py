import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import camworld as W, config, relay, sequencer as S

W._log = lambda msg: None


def q(pitch=0.0, yaw=0.0, roll=0.0):
    return S.euler_yxz_deg_to_quat(pitch, yaw, roll)


def key(t, pos, yaw=0.0, fov=60.0, lin=False):
    return S.Key(t=t, pos=tuple(pos), quat=q(yaw=yaw), fov=fov, lin=lin)


class MathTests(unittest.TestCase):
    def test_euler_round_trip(self):
        for pitch, yaw, roll in ((10, 30, 5), (-40, 170, -20), (0, -90, 0), (85, 45, 10)):
            back = S.quat_to_euler_yxz_deg(q(pitch, yaw, roll))
            for a, b in zip(back, (pitch, yaw, roll)):
                self.assertAlmostEqual(a, b, places=3)

    def test_ease_profile(self):
        for a, b in ((0, 0), (0.35, 0.35), (0.5, 0.2), (0.1, 0.5)):
            self.assertAlmostEqual(S.ease_profile(0, a, b), 0.0)
            self.assertAlmostEqual(S.ease_profile(1, a, b), 1.0)
            prev = -1
            for i in range(101):
                v = S.ease_profile(i / 100, a, b)
                self.assertGreaterEqual(v, prev - 1e-9)
                prev = v
        self.assertAlmostEqual(S.ease_profile(0.37, 0, 0), 0.37)

    def test_matrix_quat_round_trip(self):
        for _ in range(50):
            qq = relay.q_norm(tuple(random.uniform(-1, 1) for _ in range(4)))
            m = W.quat_to_mat(qq)
            self.assertLess(W.rot_error_deg(m, W.quat_to_mat(W.mat_to_quat(m))), 1e-4)


class ShotTests(unittest.TestCase):
    def shot(self, **kw):
        s = S.Shot(duration=6.0, ease_in=0.0, ease_out=0.0, **kw)
        s.keys = [key(0, (0, 0, 0)), key(2, (10, 0, 0), yaw=90, fov=40),
                  key(4, (10, 0, 10)), key(6, (0, 5, 10), fov=80)]
        return s

    def test_passes_through_keys(self):
        s = self.shot()
        for k in s.keys:
            pos, quat, fov = s.sample(k.t)
            self.assertLess(math.dist(pos, k.pos), 1e-6)
            self.assertAlmostEqual(fov, k.fov, places=6)
            self.assertLess(W.rot_error_deg(W.quat_to_mat(quat), W.quat_to_mat(k.quat)), 1e-3)

    def test_holds_outside_keys(self):
        s = self.shot()
        self.assertEqual(s.sample(-1)[0], s.keys[0].pos)
        self.assertEqual(s.sample(99)[0], s.keys[-1].pos)

    def test_straight_line_has_no_overshoot(self):
        s = S.Shot(duration=3, ease_in=0, ease_out=0)
        s.keys = [key(0, (0, 0, 0)), key(1, (1, 0, 0)), key(3, (5, 0, 0))]
        xs = [s.sample(i / 50 * 3)[0][0] for i in range(51)]
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(xs, xs[1:])))
        self.assertTrue(all(-1e-9 <= x <= 5 + 1e-9 for x in xs))

    def test_linear_flag_and_smooth_off(self):
        s = self.shot(smooth=False)
        pos, _, _ = s.sample(1.0)
        self.assertLess(math.dist(pos, (5, 0, 0)), 1e-6)
        s = self.shot()
        s.keys[0].lin = True
        pos, _, fov = s.sample(1.0)
        self.assertLess(math.dist(pos, (5, 0, 0)), 1e-6)
        self.assertAlmostEqual(fov, 50.0)

    def test_ease_shapes_time_but_keeps_ends(self):
        s = self.shot()
        s.ease_in, s.ease_out = 0.35, 0.35
        self.assertLess(math.dist(s.sample(0)[0], s.keys[0].pos), 1e-6)
        self.assertLess(math.dist(s.sample(6)[0], s.keys[-1].pos), 1e-6)
        self.assertLess(s.sample(0.3)[0][0], self.shot().sample(0.3)[0][0])

    def test_retime_constant_speed(self):
        s = S.Shot(duration=10)
        s.keys = [key(0, (0, 0, 0)), key(1, (1, 0, 0)), key(10, (10, 0, 0))]
        s.retime_constant_speed()
        self.assertAlmostEqual(s.keys[1].t, 1.0)
        s.keys = [key(0, (0, 0, 0)), key(5, (1, 0, 0)), key(10, (10, 0, 0))]
        s.retime_constant_speed()
        self.assertAlmostEqual(s.keys[1].t, 1.0)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.e = S.SequencerEngine()
        self.e.seq.shots[0].ease_in = self.e.seq.shots[0].ease_out = 0.0

    def pose(self, x):
        return {"pos": (x, 0, 0), "quat": q(), "fov": 60.0}

    def test_add_keys_spacing_undo_redo(self):
        e = self.e
        e.add_key(self.pose(0)); e.add_key(self.pose(5)); e.add_key(self.pose(9))
        self.assertEqual([k.t for k in e.shot.keys], [0.0, 2.0, 4.0])
        self.assertEqual(e.key_index, 2)
        e.undo()
        self.assertEqual(len(e.shot.keys), 2)
        e.redo()
        self.assertEqual(len(e.shot.keys), 3)

    def test_wall_clock_playback_speed_and_end_hold(self):
        e = self.e
        e.add_key(self.pose(0)); e.add_key(self.pose(10))
        e.shot.duration = 2.0
        e.play(now=100.0)
        self.assertAlmostEqual(e.sample(now=101.0)["pos"][0], 5.0, places=5)
        e.set_speed(2.0, now=101.0)
        self.assertAlmostEqual(e.time(now=101.25), 1.5)
        out = e.sample(now=105.0)
        self.assertEqual(out["pos"][0], 10.0)
        self.assertEqual(e.state, "paused")
        e.stop()
        self.assertIsNone(e.sample())

    def test_loop_wraps(self):
        e = self.e
        e.add_key(self.pose(0)); e.add_key(self.pose(10))
        e.shot.duration = 2.0
        e.set_loop(True)
        e.play(now=0.0)
        self.assertAlmostEqual(e.time(now=5.0), 1.0)
        self.assertEqual(e.state, "playing")

    def test_multi_shot_and_modes(self):
        e = self.e
        e.add_key(self.pose(0)); e.add_key(self.pose(1))
        e.add_shot()
        e.set_shot_field("mode", "car")
        e.add_key({"pos": (0.0, 2.0, -6.0), "quat": q(), "fov": 60.0})
        e.play(now=0.0)
        out = e.sample(now=0.5)
        self.assertEqual(out["space"], "car")
        self.assertEqual(out["camera_target"], "driver")
        out = e.sample(now=e.seq.shots[0].duration + 0.1)
        self.assertEqual(out["camera_target"], "follow_low")

    def test_fly_to_key_glides_then_holds(self):
        e = self.e
        e.add_key(self.pose(0)); e.add_key(self.pose(10))
        e.scrub(0.0)
        e.sample(now=0.0)
        self.assertTrue(e.fly_to_key(1, now=1.0))
        mid = e.sample(now=1.0 + S.FLY_SECONDS / 2)["pos"][0]
        self.assertTrue(0 < mid < 10)
        self.assertEqual(e.sample(now=1.0 + S.FLY_SECONDS + 0.01)["pos"][0], 10.0)

    def test_move_key_keeps_selection(self):
        e = self.e
        for x in (0, 1, 2):
            e.add_key(self.pose(x))
        e.key_index = 0
        idx = e.move_key(0, 5.0)
        self.assertEqual(idx, 2)
        self.assertEqual(e.shot.keys[idx].pos[0], 0)


class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            real = config.data_dir
            config.data_dir = lambda: Path(d)
            try:
                seq = S.Sequence(name="My Move", loop=True)
                seq.shots[0].keys = [key(0, (1, 2, 3), yaw=30, fov=45, lin=True)]
                S.save_named(seq)
                (name, path), = S.list_named()
                self.assertEqual(name, "My Move")
                back = S.load_path(path)
                k = back.shots[0].keys[0]
                self.assertEqual(back.loop, True)
                self.assertEqual(k.pos, (1.0, 2.0, 3.0))
                self.assertAlmostEqual(k.ypr[0], 30.0, places=3)
                self.assertTrue(k.lin)
                S.save_autosave(seq)
                self.assertIsNotNone(S.load_autosave())
            finally:
                config.data_dir = real

    def test_bad_files_are_cleaned(self):
        seq = S.Sequence.from_dict({"shots": [{"mode": "banana", "duration": -5,
                                               "keys": [{"t": "x", "pos": [1, "nan"], "fov": 999}]}]})
        shot = seq.shots[0]
        self.assertEqual(shot.mode, "world")
        self.assertGreaterEqual(shot.duration, 0.1)
        self.assertEqual(shot.keys[0].fov, 120.0)


class PlaybackClockTests(unittest.TestCase):
    def test_server_tick_does_not_jump_playback_to_the_end(self):
        import time
        from horizonlens import server
        srv = server.HorizonServer(None, ("127.0.0.1", 8989))
        sent = []

        class Link:
            def send_pose(self, pose):
                sent.append(pose)
        srv.link = Link()
        srv.sequencer = eng = S.SequencerEngine()
        eng.set_shot_field("mode", "car")
        eng.add_key({"pos": (0, 0, 0), "quat": q(), "fov": 60.0})
        eng.add_key({"pos": (5, 0, 0), "quat": q(), "fov": 60.0})
        eng.set_shot_field("duration", 10.0)
        eng.play()
        srv._send_sequencer_pose(time.time())
        self.assertEqual(eng.state, "playing")
        self.assertLess(eng.time(), 1.0)
        self.assertLess(sent[-1].xyz[0], 1.0)


@unittest.skipUnless(sys.platform == "win32", "Windows overlay")
class OverlayTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import ctypes, os
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QWidget
        from horizonlens import overlay
        cls.app = QApplication.instance() or QApplication([])
        cls.ov_mod = overlay
        fake = cls.fake = QWidget()
        fake.setAttribute(Qt.WA_ShowWithoutActivating)
        fake.resize(1280, 720); fake.move(-4000, -3000); fake.show()
        fh = cls.fh = int(fake.winId())

        def rect():
            pt = ctypes.wintypes.POINT(0, 0)
            ctypes.windll.user32.ClientToScreen(fh, ctypes.byref(pt))
            return fh, pt.x, pt.y, 1280, 720
        cls._saved = (overlay.game_client_rect, overlay.game.game_pid_cached)
        overlay.game_client_rect = rect
        overlay.game.game_pid_cached = lambda *a, **k: os.getpid()

    @classmethod
    def tearDownClass(cls):
        cls.ov_mod.game_client_rect, cls.ov_mod.game.game_pid_cached = cls._saved
        cls.fake.close()

    def make(self):
        class Orch:
            sequencer = S.SequencerEngine()
            running, link, server = True, object(), None
        ov = self.ov_mod.SequencerOverlay(Orch())
        self.addCleanup(ov.shutdown)
        ov.launch()
        for _ in range(5):
            self.app.processEvents()
        return ov

    def test_shown_owned_and_never_activated(self):
        import ctypes
        ov = self.make()
        u32 = ctypes.windll.user32
        u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u32.SendMessageW.restype = ctypes.c_ssize_t
        u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        hwnd = int(ov.win.winId())
        self.assertTrue(ov.win.isVisible() and ov.hud.isVisible())
        self.assertEqual(u32.GetWindowLongPtrW(hwnd, -8), self.fh)
        self.assertEqual(u32.SendMessageW(hwnd, 0x21, hwnd, (0x201 << 16) | 1), 3)
        self.assertTrue(ov.hook._thread.is_alive())

    def test_typing_goes_to_the_field(self):
        ov = self.make()
        ov._begin_edit(ov.panel.shotName)
        for ch in "Pan":
            ov.on_typed(ord(ch.upper()), ch, ch.isupper(), False)
        ov.on_typed(0x0D, "", False, False)
        self.app.processEvents()
        self.assertEqual(ov.eng.shot.name, "Pan")
        ov._begin_edit(ov.panel.shotName)
        ov.on_typed(ord("X"), "x", False, False)
        ov.on_typed(0x1B, "", False, False)
        self.assertEqual(ov.eng.shot.name, "Pan")
        self.assertFalse(ov.hook.editing)

    def test_shutdown_leaves_nothing_open(self):
        ov = self.make()
        ov.shutdown()
        self.app.processEvents()
        self.assertFalse(any(w.isVisible() for w in (ov.win, ov.hud, ov.cross)))
        self.assertIsNone(ov.hook._thread)


if __name__ == "__main__":
    unittest.main()
