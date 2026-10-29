import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import relay, server


class SpeedCurveTests(unittest.TestCase):
    def test_the_requested_points(self):
        f = relay.gap_speed_factor
        self.assertEqual(f(0), 1.0)
        self.assertEqual(f(99), 1.0)
        self.assertAlmostEqual(f(100), 1.0)
        self.assertAlmostEqual(f(200), 0.5)
        self.assertAlmostEqual(f(250), 0.3)
        self.assertEqual(f(320), 0.3)

    def test_smooth_and_never_backwards(self):
        prev, steps = 1.0, []
        for tenth in range(1000, 2501):
            v = relay.gap_speed_factor(tenth / 10)
            self.assertLessEqual(v, prev + 1e-12)
            steps.append(prev - v)
            prev = v
        self.assertLess(max(steps), 0.001)

    def test_phone_zoom_round_trip(self):
        for f in (0.5, 0.75, 1.0, 1.6, 3.0):
            self.assertAlmostEqual(relay.phone_zoom_to_factor(relay.factor_to_phone_zoom(f)), f)
        self.assertAlmostEqual(relay.factor_to_phone_zoom(1.0), 0.2)


class LensTests(unittest.TestCase):
    def test_widens_then_pulls_back(self):
        fov, pull = relay.gap_lens(1.0, 70.0, 4.5)
        self.assertAlmostEqual(fov, 70.0)
        self.assertEqual(pull, 0.0)
        fov, pull = relay.gap_lens(0.5, 70.0, 4.5)
        self.assertAlmostEqual(fov, 120.0)
        self.assertEqual(pull, 0.0)
        fov, pull = relay.gap_lens(0.3, 70.0, 4.5)
        self.assertAlmostEqual(fov, 120.0)
        self.assertGreater(pull, 1.0)
        self.assertLessEqual(pull, relay.GAP_MAX_PULL)

    def test_pull_back_goes_along_the_view(self):
        out = relay.pull_back({"xyz": (0.0, 1.0, -3.0), "quaternion": (0.0, 0.0, 0.0, 1.0)}, 2.0)
        self.assertEqual(out["xyz"], (0.0, 1.0, -5.0))

    def test_easing_never_jumps(self):
        z = relay.GapZoom()
        z.update(1.0, 0.0)
        v = z.update(0.3, 1 / 90)
        self.assertGreater(v, 0.97)
        t = 1 / 90
        for _ in range(int(4 * 90)):
            t += 1 / 90
            v = z.update(0.3, t)
        self.assertAlmostEqual(v, 0.3, places=2)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.srv = server.HorizonServer(None, ("127.0.0.1", 8989))
        self.sent = []
        sent = self.sent

        class Link:
            def send_pose(self, p):
                sent.append(p)
        self.srv.link = Link()
        self.srv._framing.path = None
        self.srv._framing.selected = relay.FRAME_GAP
        self.srv._framing.offsets[relay.FRAME_GAP] = [0.0, 0.0, 0.0]
        self.mps = 0.0
        self.srv.speed_source = lambda: self.mps

    def tick(self, now, dynamic=True):
        sample = {"euler": (0.0, 0.0, 0.0), "zoom": 0.2, "fov_base": 70.0, "position": (0.0, 0.0, 0.0),
                  "gap_dynamic": dynamic, "tracking": True, "freecam": False}
        self.srv._send_pose(sample, now, now, True)
        return self.sent[-1]

    def test_parked_is_the_phone_zoom(self):
        p = self.tick(0.0)
        self.assertAlmostEqual(p.fov, 70.0, places=3)
        self.assertEqual(self.srv.gap_info["zoom"], 1.0)

    def test_fast_zooms_out_and_pulls_back(self):
        parked = self.tick(0.0)
        self.mps = 250 / relay.MPS_TO_MPH
        t, p = 0.0, None
        for _ in range(5 * 90):
            t += 1 / 90
            p = self.tick(t)
        self.assertAlmostEqual(p.fov, 120.0, places=3)
        self.assertLess(p.xyz[2], parked.xyz[2] - 1.0)
        self.assertAlmostEqual(self.srv.gap_info["zoom"], 0.3, places=2)
        self.assertEqual(self.srv.gap_info["mph"], 250)

    def test_off_or_other_presets_leave_the_lens_alone(self):
        self.mps = 250 / relay.MPS_TO_MPH
        self.assertAlmostEqual(self.tick(0.0, dynamic=False).fov, 70.0, places=3)
        self.srv._framing.selected = relay.FRAME_BUMPER
        self.assertAlmostEqual(self.tick(0.1).fov, 70.0, places=3)
        self.assertIsNone(self.srv.gap_info)

    def test_speed_falls_back_to_telemetry(self):
        self.srv.speed_source = lambda: None
        import time
        self.srv._telemetry = {"received_at": time.time(), "speed": 40.0}
        self.assertEqual(self.srv.speed_mps(), 40.0)
        self.srv._telemetry["received_at"] -= 5
        self.assertIsNone(self.srv.speed_mps())


if __name__ == "__main__":
    unittest.main()
