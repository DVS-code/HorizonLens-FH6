import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import camworld as W, rcf6, recording, seqanim, sequencer as S

W._log = lambda msg: None


def q(yaw=0.0):
    return S.euler_yxz_deg_to_quat(0.0, yaw, 0.0)


def pose(x, fov=60.0):
    return {"pos": (x, 0.0, 0.0), "quat": q(), "fov": fov}


def engine_with_keys(xs=(0, 10, 20, 30)):
    e = S.SequencerEngine()
    e.shot.ease_in = e.shot.ease_out = 0.0
    for x in xs:
        e.add_key(pose(x))
    return e


class ClipEditTests(unittest.TestCase):
    def sample_all(self, e, n=61):
        out = []
        total = e.seq.duration
        for i in range(n):
            t = total * i / (n - 1)
            idx, start = e.seq.locate(t)
            out.append(e.seq.shots[idx].sample(t - start)[0])
        return out

    def test_split_keeps_the_move_seamless(self):
        e = engine_with_keys()
        e.shot.ease_in = e.shot.ease_out = 0.3
        before = self.sample_all(e)
        self.assertTrue(e.split_shot(2.5))
        self.assertEqual(len(e.seq.shots), 2)
        a, b = e.seq.shots
        self.assertAlmostEqual(a.duration, 2.5)
        self.assertAlmostEqual(b.trim_in, 2.5)
        self.assertAlmostEqual(a.duration + b.duration, 6.0)
        self.assertEqual(b.name, "Shot 1 (2)")
        self.assertEqual(e.shot_index, 1)
        after = self.sample_all(e)
        for p0, p1 in zip(before, after):
            self.assertLess(math.dist(p0, p1), 1e-6)
        e.undo()
        self.assertEqual(len(e.seq.shots), 1)

    def test_split_refuses_the_edges(self):
        e = engine_with_keys()
        self.assertFalse(e.split_shot(0.0))
        self.assertFalse(e.split_shot(6.0))
        self.assertIn("playhead", e.message)

    def test_trim_hides_and_brings_back(self):
        e = engine_with_keys()
        e.trim_shot(0, 2.0, 2.0)
        s = e.shot
        self.assertEqual(len(s.keys), 4)
        self.assertEqual(s.sample(0.0)[0], (10.0, 0.0, 0.0))
        self.assertEqual(s.sample(2.0)[0], (20.0, 0.0, 0.0))
        self.assertEqual([s.in_window(k) for k in s.keys], [False, True, True, False])
        e.trim_shot(0, 0.0, 6.0)
        self.assertEqual(s.sample(0.0)[0] if False else e.shot.sample(6.0)[0], (30.0, 0.0, 0.0))

    def test_extending_past_the_last_key_holds(self):
        e = engine_with_keys()
        e.trim_shot(0, 0.0, 9.0)
        self.assertEqual(e.shot.sample(8.0)[0], (30.0, 0.0, 0.0))

    def test_move_shot_reorders(self):
        e = engine_with_keys()
        e.add_shot()
        e.set_shot_field("name", "Second")
        e.move_shot(1, 0)
        self.assertEqual(e.seq.shots[0].name, "Second")
        self.assertEqual(e.shot_index, 0)
        e.undo()
        self.assertEqual(e.seq.shots[1].name, "Second")

    def test_keys_added_after_a_trim_widen_the_window(self):
        e = engine_with_keys((0, 10))
        e.trim_shot(0, 0.0, 1.0)
        e.add_key(pose(20))
        self.assertAlmostEqual(e.shot.trim_out, 4.0)

    def test_table_time_is_into_the_window(self):
        e = engine_with_keys()
        e.trim_shot(0, 2.0, 4.0)
        e.set_key_field(1, "t", 0.5)
        self.assertAlmostEqual(e.shot.keys[1].t, 2.5)
        e.key_index = 1
        e.fly_to_key(1, now=0.0)
        self.assertAlmostEqual(e.time(), 0.5)

    def test_fly_to_key_stays_on_the_key_with_easing(self):
        e = engine_with_keys()
        e.shot.ease_in = e.shot.ease_out = 0.4
        e.scrub(0.0)
        e.sample(now=0.0)
        e.fly_to_key(1, now=1.0)
        for now in (1.0 + S.FLY_SECONDS + 0.01, 3.0, 5.0):
            self.assertEqual(e.sample(now=now)["pos"], (10.0, 0.0, 0.0))
        e.scrub(e.time())
        self.assertNotEqual(e.sample(now=6.0)["pos"], (10.0, 0.0, 0.0))

    def test_round_trip_and_old_files(self):
        e = engine_with_keys()
        e.trim_shot(0, 1.0, 3.0)
        back = S.Sequence.from_dict(e.seq.to_dict())
        self.assertAlmostEqual(back.shots[0].trim_in, 1.0)
        self.assertAlmostEqual(back.shots[0].duration, 3.0)
        old = S.Sequence.from_dict({"shots": [{"duration": 1.0, "keys": [
            {"t": 0, "pos": [0, 0, 0]}, {"t": 5, "pos": [1, 0, 0]}]}]})
        self.assertAlmostEqual(old.shots[0].duration, 5.0)


class TriggerTests(unittest.TestCase):
    def test_toggles_follow_the_playhead(self):
        e = engine_with_keys()
        e.add_trigger("togglehood", 1.0)
        e.add_trigger("togglehood", 3.0)
        e.add_trigger("lightsall", 2.0)
        self.assertEqual(e.animation_state(0.5), {"togglehood": False, "lightsall": False})
        self.assertEqual(e.animation_state(1.0), {"togglehood": True, "lightsall": False})
        self.assertEqual(e.animation_state(2.5), {"togglehood": True, "lightsall": True})
        self.assertEqual(e.animation_state(5.0), {"togglehood": False, "lightsall": True})

    def test_one_shots_are_not_states(self):
        e = engine_with_keys()
        e.add_trigger("resetstate", 2.0)
        self.assertEqual(e.animation_state(3.0), {})
        self.assertEqual(e.seq.one_shots_between(1.0, 2.0), ["resetstate"])
        self.assertEqual(e.seq.one_shots_between(2.0, 3.0), [])

    def test_edit_move_delete_undo_and_save(self):
        e = engine_with_keys()
        e.add_trigger("togglehood", 1.0)
        e.add_trigger("toggletrunk", 4.0)
        e.move_trigger(0, 5.0)
        self.assertEqual([g.action for g in e.seq.triggers], ["toggletrunk", "togglehood"])
        self.assertEqual(e.trigger_index, 1)
        e.add_trigger("togglehood", 99.0)
        self.assertLessEqual(e.seq.triggers[-1].t, e.seq.duration)
        back = S.Sequence.from_dict(e.seq.to_dict())
        self.assertEqual([(g.t, g.action) for g in back.triggers],
                         [(g.t, g.action) for g in e.seq.triggers])
        e.delete_trigger(0)
        self.assertEqual(len(e.seq.triggers), 2)
        e.undo()
        self.assertEqual(len(e.seq.triggers), 3)
        bad = S.Sequence.from_dict({"triggers": [{"t": 1, "action": "format_c"}, "x"]})
        self.assertEqual(bad.triggers, [])


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class AnimationDriverTests(unittest.TestCase):
    def setUp(self):
        self.e = engine_with_keys()
        self.e.add_trigger("togglehood", 1.0)
        self.e.add_trigger("togglehood", 3.0)
        self.presses = []
        self.ok = True
        self.clock = FakeClock()

        def press(action):
            self.presses.append(action)
            return (True, "sent") if self.ok else (False, "Forzavista isn't running")
        self.d = seqanim.AnimationDriver(self.e, press=press, clock=self.clock)

    def play_to(self, t, step=0.05):
        while self.e.time(now=self._now) < t:
            self._now += step
            self.clock.t += step
            self.e.time = self._time
            self.d.tick()

    def _time(self, now=None):
        return S.SequencerEngine.time(self.e, self._now)

    def test_playback_presses_on_each_trigger(self):
        self._now = 0.0
        self.e.play(now=0.0)
        self.play_to(4.0)
        self.assertEqual(self.presses, ["togglehood", "togglehood"])
        self.assertEqual(self.d.snapshot(), {})

    def test_scrub_back_restores_default_after_settling(self):
        self.e.scrub(2.0)
        self.d.tick()
        self.assertEqual(self.presses, [])
        self.clock.t += seqanim.SETTLE_SECONDS + 0.01
        self.d.tick()
        self.assertEqual(self.presses, ["togglehood"])
        self.e.scrub(0.5)
        self.d.tick()
        self.clock.t += seqanim.SETTLE_SECONDS + 0.01
        self.d.tick()
        self.assertEqual(self.presses, ["togglehood", "togglehood"])
        self.assertEqual(self.d.snapshot(), {})

    def test_failure_backs_off_and_explains(self):
        self.ok = False
        self.e.scrub(2.0)
        self.d.tick()
        self.clock.t += 1.0
        self.d.tick()
        self.d.tick()
        self.assertEqual(len(self.presses), 1)
        self.assertIn("Forzavista", self.e.message)

    def test_stopped_or_no_triggers_does_nothing(self):
        self.e.stop()
        self.clock.t += 5
        self.d.tick()
        self.assertEqual(self.presses, [])

    def test_new_game_forgets(self):
        self.e.scrub(2.0)
        self.d.tick(game_pid=10)
        self.clock.t += 1.0
        self.d.tick(game_pid=10)
        self.assertEqual(self.d.snapshot(), {"togglehood": True})
        self.d.tick(game_pid=11)
        self.assertEqual(self.d.snapshot(), {})
        self.clock.t += 1.0
        self.d.tick(game_pid=11)
        self.assertEqual(self.presses.count("togglehood"), 2)


class ZoomTests(unittest.TestCase):
    def test_matches_the_phone_curve(self):
        self.assertAlmostEqual(S.zoom_to_fov(1.0), 70.0)
        self.assertAlmostEqual(S.zoom_to_fov(0.5), 120.0)
        self.assertAlmostEqual(S.zoom_to_fov(3.0), 15.0)
        for z in (0.5, 0.8, 1.0, 1.7, 2.4, 3.0):
            self.assertAlmostEqual(S.fov_to_zoom(S.zoom_to_fov(z)), z, places=4)

    def test_fly_zoom_keys_and_speed_cap(self):
        from horizonlens import overlay
        f = overlay.FlyCam()
        f.start_from(None)
        for _ in range(60):
            f.step(1 / 60, {"ZIN": True})
        self.assertAlmostEqual(f.zoom, f.ZOOM_RATE, places=3)
        self.assertLess(f.sample()["fov"], 70.0)
        f.speed = 29.0
        self.assertEqual(f.effective_speed({"SHIFT": True}), overlay.FlyCam.MAX_SPEED)
        f.start_from({"pos": (0, 1, -4), "quat": q(), "fov": S.zoom_to_fov(2.0)})
        self.assertAlmostEqual(f.zoom, 2.0, places=4)


class PacketNumberTests(unittest.TestCase):
    def test_every_packet_gets_a_fresh_sequence(self):
        link = rcf6.CompanionLink(bytes(32))
        sent = []

        class Sock:
            def sendto(self, data, addr):
                sent.append(data)
                return len(data)

            def close(self):
                pass
        link._sock.close()
        link._sock = Sock()
        link._probed = True
        self.assertEqual(rcf6.Pose().sequence, 0)
        for kind in (1, 7, 2):
            link.send_pose(rcf6.Pose(kind=kind))
        seqs = [int.from_bytes(d[12:16], "little") for d in sent]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertTrue(all(seqs))


class RecorderCleanupTests(unittest.TestCase):
    def test_stop_tidies_a_recording_that_ended_by_itself(self):
        r = recording.Recorder()

        class Proc:
            returncode = 1

            def poll(self):
                return 1

            class stdin:
                @staticmethod
                def close():
                    pass

        class Log:
            closed = False

            def close(self):
                Log.closed = True
        r._proc = Proc()
        r._log = Log()
        r.current_path = Path("does-not-exist.mp4")
        self.assertIsNone(r.stop())
        self.assertTrue(Log.closed)
        self.assertIsNone(r._proc)
        self.assertIn("by itself", r.last_error)


class TokenTests(unittest.TestCase):
    def test_busy_phones_keep_their_pairing(self):
        import asyncio
        from horizonlens import server
        srv = server.HorizonServer(None, ("127.0.0.1", 8989))

        class Req:
            def __init__(self, path, token=""):
                self.path = path
                self.headers = {"X-HL-Token": token} if token else {}
                self.query = {"code": srv.pair_code}

        async def ok(_req):
            return "ok"

        async def run():
            first = (await srv.handle_pair(Req("/pair"))).text
            import json
            token = json.loads(first)["token"]
            for _ in range(80):
                await srv.handle_pair(Req("/pair"))
                self.assertEqual(await srv._require_pairing(Req("/health", token), ok), "ok")
        asyncio.run(run())


class BackgroundResultTests(unittest.TestCase):
    def test_done_runs_on_the_ui_thread(self):
        import threading
        import time as _t
        from PySide6.QtWidgets import QApplication
        from horizonlens import desktop
        app = QApplication.instance() or QApplication([])
        relay = desktop._BgRelay()
        seen = []
        threading.Thread(target=lambda: relay.done.emit(
            lambda r: seen.append((r, threading.current_thread() is threading.main_thread())),
            42)).start()
        until = _t.time() + 2
        while not seen and _t.time() < until:
            app.processEvents()
            _t.sleep(0.01)
        self.assertEqual(seen, [(42, True)])


@unittest.skipUnless(sys.platform == "win32", "Windows overlay")
class OverlayEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from horizonlens import overlay
        cls.app = QApplication.instance() or QApplication([])
        cls.O = overlay
        cls._saved = overlay.game_client_rect
        overlay.game_client_rect = lambda: None

    @classmethod
    def tearDownClass(cls):
        cls.O.game_client_rect = cls._saved

    def make(self):
        e = engine_with_keys()
        e.add_shot()
        e.add_key(pose(50)); e.add_key(pose(60))
        e.select_shot(0)

        class Orch:
            sequencer = e
            running, link, server, anim = True, object(), None, None
        ov = self.O.SequencerOverlay(Orch())
        self.addCleanup(ov.shutdown)
        ov.panel.refresh()
        self.app.processEvents()
        tl = ov.panel.timeline
        self.assertGreater(tl.width(), 400)
        return ov, e, tl

    def drag(self, tl, x0, y, x1):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest
        QTest.mousePress(tl, self.O.Qt.LeftButton, self.O.Qt.NoModifier, QPoint(int(x0), int(y)))
        for i in range(1, 6):
            QTest.mouseMove(tl, QPoint(int(x0 + (x1 - x0) * i / 5), int(y)))
        QTest.mouseRelease(tl, self.O.Qt.LeftButton, self.O.Qt.NoModifier, QPoint(int(x1), int(y)))

    def test_drag_right_edge_trims(self):
        ov, e, tl = self.make()
        y = tl.BAR_Y + 8
        self.drag(tl, tl.t2x(6.0) - 2, y, tl.t2x(4.0))
        self.assertAlmostEqual(e.seq.shots[0].duration, 4.0, delta=0.15)
        self.assertEqual(len(e.seq.shots[0].keys), 4)

    def test_drag_shot_moves_it(self):
        ov, e, tl = self.make()
        e.set_shot_field("name", "First")
        y = tl.BAR_Y + 8
        self.drag(tl, tl.t2x(3.0), y, tl.t2x(e.seq.duration - 0.2))
        self.assertEqual(e.seq.shots[1].name, "First")

    def test_drag_trigger_moves_it(self):
        ov, e, tl = self.make()
        e.add_trigger("togglehood", 1.0)
        ov.panel.refresh()
        self.app.processEvents()
        y = next(top + h / 2 for lane, top, h in tl.lane_rows() if lane == "togglehood")
        self.drag(tl, tl.t2x(1.0), y, tl.t2x(2.5))
        self.assertAlmostEqual(e.seq.triggers[0].t, 2.5, delta=0.1)

    def _menu_button(self, ov, text):
        self.assertTrue(ov.menu.isVisible(), "the menu didn't open")
        for b in ov.menu.buttons:
            if b.text().startswith(text):
                return b
        self.fail(f"no menu item {text!r}: {[b.text() for b in ov.menu.buttons]}")

    def test_switching_a_keyed_shot_keeps_its_keys(self):
        ov, e, tl = self.make()
        e.select_shot(1)
        n = len(e.shot.keys)
        ov.panel.modeBtns["car"].click()
        self.assertEqual(e.shot.mode, "car")
        ov.panel.modeBtns["world"].click()
        self.assertEqual(e.shot.mode, "world")
        self.assertEqual(len(e.shot.keys), n)
        self.assertFalse(ov.menu.isVisible())

    def test_menu_closes_on_outside_click_and_escape(self):
        ov, e, tl = self.make()
        ov.panel._library_menu(ov.panel.nameEdit)
        self.assertTrue(ov.menu.isVisible())
        self.assertTrue(ov.hook.menu_open)
        ov.on_press_with_menu(-99999, -99999)
        self.assertFalse(ov.menu.isVisible())
        ov.panel._library_menu(ov.panel.nameEdit)
        ov.close_menu()
        self.assertFalse(ov.menu.isVisible() or ov.hook.menu_open)

    def test_library_delete_needs_two_clicks(self):
        import tempfile
        from horizonlens import config
        real = config.data_dir
        d = tempfile.mkdtemp()
        config.data_dir = lambda: Path(d)
        try:
            ov, e, tl = self.make()
            e.set_name("Keep me")
            S.save_named(e.seq)
            ov.panel._library_menu(ov.panel.nameEdit)
            b = self._menu_button(ov, "Delete “Keep me”")
            b.click()
            self.app.processEvents()
            self.assertEqual(len(S.list_named()), 1)
            self.assertTrue(b.text().startswith("Click again"))
            b.click()
            for _ in range(3):
                self.app.processEvents()
            self.assertEqual(S.list_named(), [])
        finally:
            config.data_dir = real

    def test_hook_lets_our_forzavista_presses_through(self):
        import time as _t
        from horizonlens import vista
        ov, e, tl = self.make()
        seen = []
        ov.hook.hot.connect(seen.append)
        ov.hook._active = lambda: True
        saved = dict(self.O.HOTKEY_ACTIONS)
        self.O.HOTKEY_ACTIONS[0x87] = "play"
        ov.hook.install()
        try:
            def press(mark):
                down = vista._key(0x87, False); up = vista._key(0x87, True)
                if not mark:
                    down.ki.dwExtraInfo = up.ki.dwExtraInfo = 0
                arr = (vista._INPUT * 1)(down)
                vista._u32.SendInput(1, arr, self.O.ctypes.sizeof(vista._INPUT))
                _t.sleep(0.05)
                held = bool(self.O._u32.GetAsyncKeyState(0x87) & 0x8000)
                arr = (vista._INPUT * 1)(up)
                vista._u32.SendInput(1, arr, self.O.ctypes.sizeof(vista._INPUT))
                _t.sleep(0.05)
                self.app.processEvents()
                return held
            self.assertFalse(press(mark=False))
            self.assertEqual(seen, ["play"])
            self.assertTrue(press(mark=True))
            self.assertEqual(seen, ["play"])
        finally:
            self.O.HOTKEY_ACTIONS.clear()
            self.O.HOTKEY_ACTIONS.update(saved)
            ov.hook.remove()

    def test_dropped_hook_is_put_back(self):
        import os
        ov, e, tl = self.make()
        ov.launched = True
        calls = []
        ov.hook.reinstall = lambda: calls.append("rehook")
        ov.hotkey = lambda action: calls.append(action)
        saved = (self.O._foreground_pid, self.O._key)
        self.O._foreground_pid = lambda: os.getpid()
        self.O._key = lambda vk: vk == 0x75
        try:
            ov._check_hook(None)
            ov._check_hook(None)
        finally:
            self.O._foreground_pid, self.O._key = saved
        self.assertEqual(calls, ["rehook", "play"])


if __name__ == "__main__":
    unittest.main()
