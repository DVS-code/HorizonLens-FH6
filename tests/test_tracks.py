import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import airride, curves, seqanim, sequencer as S


def q():
    return S.euler_yxz_deg_to_quat(0.0, 0.0, 0.0)


def engine(seconds=10.0):
    e = S.SequencerEngine()
    e.add_key({"pos": (0, 0, 0), "quat": q(), "fov": 70.0})
    e.add_key({"pos": (10, 0, 0), "quat": q(), "fov": 70.0})
    e.set_shot_field("duration", seconds)
    return e


class CurveTests(unittest.TestCase):
    def test_ends_are_fixed_and_shape_is_kept(self):
        c = curves.normalise([0.3, 0.5, 0.2])
        self.assertEqual(len(c), curves.POINTS)
        self.assertEqual((c[0], c[-1]), (0.0, 1.0))
        self.assertEqual(curves.normalise("junk"), curves.DEFAULT_CURVE)
        self.assertAlmostEqual(curves.value(curves.PRESETS["linear"], 0.37), 0.37)
        self.assertEqual(airride.CAMBER_CURVES["smooth"], curves.DEFAULT_CURVE)


class ZoomTrackTests(unittest.TestCase):
    def test_before_first_keyframe_the_keys_zoom_applies(self):
        e = engine()
        e.add_trigger("zoom", 2.0, 2.0)
        self.assertIsNone(e.seq.zoom_at(1.0))
        e.scrub(1.0)
        self.assertAlmostEqual(e.sample()["fov"], 70.0)

    def test_eases_to_each_keyframe_over_the_transition(self):
        e = engine()
        e.set_setting("zoom", "curve", curves.PRESETS["linear"])
        e.set_setting("zoom", "transition", 2.0)
        e.add_trigger("zoom", 2.0, 2.0)
        e.add_trigger("zoom", 6.0, 0.5)
        self.assertAlmostEqual(e.seq.zoom_at(3.0), 1.5)
        self.assertAlmostEqual(e.seq.zoom_at(5.0), 2.0)
        self.assertAlmostEqual(e.seq.zoom_at(7.0), 1.25)
        e.scrub(9.0)
        self.assertAlmostEqual(e.sample()["fov"], S.zoom_to_fov(0.5))

    def test_a_keyframe_mid_transition_starts_from_where_it_got(self):
        e = engine()
        e.set_setting("zoom", "curve", curves.PRESETS["linear"])
        e.set_setting("zoom", "transition", 4.0)
        e.add_trigger("zoom", 0.0, 3.0)
        e.add_trigger("zoom", 2.0, 1.0)
        self.assertAlmostEqual(e.seq.zoom_at(2.0), 2.0)
        self.assertAlmostEqual(e.seq.zoom_at(4.0), 1.5)

    def test_instant_and_switched_off(self):
        e = engine()
        e.set_setting("zoom", "transition", 0.0)
        e.add_trigger("zoom", 2.0, 2.5)
        self.assertAlmostEqual(e.seq.zoom_at(2.0), 2.5)
        e.set_setting("zoom", "enabled", False)
        self.assertIsNone(e.seq.zoom_at(5.0))

    def test_keyframe_value_edit_and_save(self):
        e = engine()
        e.add_trigger("zoom", 1.0, 2.0)
        e.set_trigger_value(e.trigger_index, 9.0)
        self.assertEqual(e.selected_trigger().value, S.ZOOM_MAX)
        back = S.Sequence.from_dict(e.seq.to_dict())
        self.assertEqual(back.triggers[0].value, S.ZOOM_MAX)


class AirTrackTests(unittest.TestCase):
    def setUp(self):
        self.e = engine()
        self.e.set_setting("air", "curve", curves.PRESETS["linear"])
        self.e.set_setting("air", "camber_curve", curves.PRESETS["linear"])
        self.e.set_setting("air", "transition", 2.0)

    def test_toggles_drop_then_raise(self):
        e = self.e
        e.add_trigger("air", 1.0)
        e.add_trigger("air", 5.0)
        h, cam = e.seq.air_at(0.5)
        self.assertEqual(h, [0.0] * 4)
        self.assertIsNone(cam)
        self.assertEqual(e.seq.air_at(2.0)[0], [0.5] * 4)
        self.assertEqual(e.seq.air_at(4.0)[0], [1.0] * 4)
        self.assertEqual(e.seq.air_at(6.0)[0], [0.5] * 4)
        self.assertEqual(e.seq.air_at(8.0)[0], [0.0] * 4)

    def test_axle_order_reverses_on_the_raise(self):
        e = self.e
        e.set_setting("air", "sequence", "front")
        e.add_trigger("air", 0.0)
        e.add_trigger("air", 4.0)
        h, _ = e.seq.air_at(0.4)
        self.assertGreater(h[0], 0.0)
        self.assertEqual(h[2], 0.0)
        h, _ = e.seq.air_at(4.4)
        self.assertLess(h[2], 1.0)
        self.assertEqual(h[0], 1.0)

    def test_camber_control(self):
        e = self.e
        e.set_setting("air", "camber", True)
        e.add_trigger("air", 0.0)
        h, cam = e.seq.air_at(1.0)
        self.assertEqual(cam, [0.5] * 4)

    def test_off_and_empty(self):
        e = self.e
        self.assertIsNone(e.seq.air_at(1.0))
        e.add_trigger("air", 0.0)
        e.set_setting("air", "enabled", False)
        self.assertIsNone(e.seq.air_at(1.0))


class SettingsTests(unittest.TestCase):
    def test_clean_and_round_trip(self):
        e = engine()
        e.set_setting("air", "drop", 500)
        e.set_setting("air", "sequence", "sideways")
        e.set_setting("nope", "x", 1)
        self.assertEqual(e.seq.settings["air"]["drop"], 90.0)
        self.assertEqual(e.seq.settings["air"]["sequence"], "together")
        back = S.Sequence.from_dict(e.seq.to_dict())
        self.assertEqual(back.settings, e.seq.settings)
        old = S.Sequence.from_dict({"shots": []})
        self.assertEqual(old.settings, S.default_settings())

    def test_reset_and_undo(self):
        e = engine()
        e.set_setting("zoom", "transition", 5.0)
        e.reset_settings("zoom")
        self.assertEqual(e.seq.settings["zoom"]["transition"], 1.0)
        e.undo()
        self.assertEqual(e.seq.settings["zoom"]["transition"], 5.0)

    def test_forzavista_switch(self):
        e = engine()
        e.add_trigger("togglehood", 1.0)
        self.assertEqual(e.animation_state(2.0), {"togglehood": True})
        e.set_setting("animations", "enabled", False)
        self.assertEqual(e.animation_state(2.0), {})


class FakeCar:
    def __init__(self):
        self.heights, self.cambers = None, None

    def set_ride_height(self, v):
        self.heights = list(v)
        return True

    def set_camber(self, v):
        self.cambers = list(v)
        return True

    ride_height = property(lambda self: self.heights)
    camber = property(lambda self: self.cambers)


class AirDriveTests(unittest.TestCase):
    def air(self):
        a = airride.AirRide()
        a.stock = [0.40, 0.40, 0.42, 0.42]
        a.radii = [0.30, 0.30, 0.32, 0.32]
        a.stock_camber = [-1.0, 1.0, 1.0, -1.0]
        a.car = FakeCar()
        return a

    def test_targets_follow_progress_and_settings(self):
        a = self.air()
        cfg = dict(S.default_settings()["air"], drop=50.0, floor=0.0, camber=True,
                   camber_front=-5.0, camber_rear=-3.0)
        h, cam = a.drive_targets(cfg, [0, 1, 0.5, 0], [0, 1, 0, 0])
        self.assertAlmostEqual(h[0], 0.40)
        self.assertAlmostEqual(h[1], 0.35)
        self.assertAlmostEqual(h[2], 0.395)
        self.assertEqual(cam[0], -1.0)
        self.assertEqual(cam[1], 5.0)
        _, cam = a.drive_targets(dict(cfg, camber=False), [0] * 4, [1] * 4)
        self.assertIsNone(cam)

    def test_camber_goes_back_when_switched_off(self):
        a = self.air()
        cfg = dict(S.default_settings()["air"], camber=True, camber_front=-8.0)
        a.drive(cfg, [1] * 4, [1] * 4)
        a._apply_drive(a._drive, now=0.0)
        self.assertEqual(a.car.cambers[0], -8.0)
        a.drive(dict(cfg, camber=False), [1] * 4, None)
        a._apply_drive(a._drive, now=0.1)
        self.assertEqual(a.car.cambers, a.stock_camber)

    def test_drive_writes_and_phone_waits(self):
        a = self.air()
        a.drive(dict(S.default_settings()["air"], drop=60.0, floor=0.0), [1] * 4)
        a._apply_drive(a._drive, now=0.0)
        self.assertAlmostEqual(a.car.heights[0], 0.34)
        self.assertEqual(a.status, "sequencer")
        a.command("up")
        self.assertEqual(a._commands, [])
        a.release(ramp_back=False)
        a.command("up")
        self.assertEqual(a._commands, ["up"])
        self.assertFalse(a.driven)


class DriverAirTests(unittest.TestCase):
    def test_engaged_sequence_drives_then_releases(self):
        e = engine()
        e.add_trigger("air", 1.0)

        class Air:
            calls = []

            def drive(self, cfg, h, c):
                self.calls.append(("drive", tuple(h)))

            def release(self, ramp_back=True):
                self.calls.append(("release", ramp_back))
        air = Air()
        d = seqanim.AnimationDriver(e, press=lambda a: (True, ""), air=air)
        d.tick()
        self.assertEqual(air.calls, [])
        e.scrub(5.0)
        d.tick()
        self.assertEqual(air.calls[-1][0], "drive")
        e.stop()
        d.tick()
        self.assertEqual(air.calls[-1], ("release", True))


@unittest.skipUnless(sys.platform == "win32", "Windows overlay")
class SettingsTabTests(unittest.TestCase):
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
        e = engine()

        class Orch:
            sequencer = e
            running, link, server, anim, air = True, object(), None, None, None
        ov = self.O.SequencerOverlay(Orch())
        self.addCleanup(ov.shutdown)
        ov.panel.refresh()
        return ov, e

    def test_slider_switch_and_curve_change_the_sequence(self):
        ov, e = self.make()
        panel = ov.panel
        sl = panel._set_w[("air", "drop")][0]
        sl.setValue(33)
        self.assertEqual(e.seq.settings["air"]["drop"], 33.0)
        panel._set_w[("air", "camber")].setChecked(True)
        self.app.processEvents()
        self.assertTrue(e.seq.settings["air"]["camber"])
        self.assertTrue(panel._set_w[("air", "camber_front")][0].isEnabled())
        ed = panel._set_w[("zoom", "curve")]
        ed.committed.emit(curves.PRESETS["late"])
        self.assertEqual(e.seq.settings["zoom"]["curve"], curves.PRESETS["late"])
        panel._set_w[("air", "enabled")].setChecked(False)
        panel.refresh()
        self.assertNotIn("air", panel.timeline.lanes())

    def test_zoom_slider_edits_a_selected_zoom_keyframe(self):
        ov, e = self.make()
        e.add_trigger("zoom", 1.0, 1.0)
        ov.set_zoom(2.2, commit=True)
        self.assertAlmostEqual(e.seq.triggers[0].value, 2.2)
        self.assertAlmostEqual(ov.shown_zoom(), 2.2)


if __name__ == "__main__":
    unittest.main()
