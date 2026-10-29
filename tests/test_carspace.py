import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import carpose, core, rcf6, relay, sequencer as S


def q(pitch=0.0, yaw=0.0):
    return S.euler_yxz_deg_to_quat(pitch, yaw, 0.0)


def car_at(x, z, heading_deg):
    h = math.radians(heading_deg)
    fwd = (math.sin(h), 0.0, math.cos(h))
    right = (math.cos(h), 0.0, -math.sin(h))
    return carpose.CarPose((x, 0.0, z), [right, (0.0, 1.0, 0.0), fwd])


def world_of(car, pos, quat):
    w = car.point_to_world(pos)
    look = car.to_world(relay_rotate(quat, (0.0, 0.0, 1.0)))
    return w, look


def relay_rotate(qq, v):
    from horizonlens import camworld
    m = camworld.quat_to_mat(qq)
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


class CarPoseTests(unittest.TestCase):
    def test_round_trips(self):
        c = car_at(100.0, -50.0, 30.0)
        p = (1.5, 2.0, -6.0)
        back = c.point_to_car(c.point_to_world(p))
        self.assertLess(math.dist(back, p), 1e-9)
        self.assertIsNotNone(carpose.CarPose.from_dict(c.to_dict()))
        self.assertIsNone(carpose.CarPose.from_dict({"pos": [0, 0, 0], "axes": [[2, 0, 0]] * 3}))


class KeyAtPlayheadTests(unittest.TestCase):
    def pose(self, x):
        return {"pos": (x, 1.0, -5.0), "quat": q(), "fov": 70.0}

    def test_adds_then_moves_on(self):
        e = S.SequencerEngine()
        self.assertEqual(e.key_at_playhead(self.pose(0)), "added")
        self.assertAlmostEqual(e.time(), 2.0)
        self.assertEqual(e.key_at_playhead(self.pose(5)), "added")
        self.assertAlmostEqual(e.time(), 4.0)
        self.assertEqual([k.t for k in e.shot.keys], [0.0, 2.0])
        self.assertGreaterEqual(e.shot.duration, 4.0)

    def test_on_a_key_it_updates(self):
        e = S.SequencerEngine()
        e.key_at_playhead(self.pose(0))
        e.key_at_playhead(self.pose(5))
        e.scrub(2.0)
        self.assertEqual(e.key_at_playhead(self.pose(9)), "updated")
        self.assertEqual(len(e.shot.keys), 2)
        self.assertEqual(e.shot.keys[1].pos[0], 9)
        self.assertAlmostEqual(e.time(), 2.0)

    def test_keys_play_back_between_two_spots(self):
        e = S.SequencerEngine()
        e.shot.ease_in = e.shot.ease_out = 0.0
        e.key_at_playhead(self.pose(0))
        e.key_at_playhead(self.pose(10))
        e.play(now=0.0)
        self.assertAlmostEqual(e.sample(now=1.0)["pos"][0], 5.0, places=3)
        self.assertAlmostEqual(e.sample(now=2.0)["pos"][0], 10.0, places=3)

    def test_mode_switch_keeps_keys(self):
        e = S.SequencerEngine()
        e.key_at_playhead(self.pose(0))
        e.set_shot_field("mode", "car")
        e.set_shot_field("mode", "world")
        self.assertEqual(len(e.shot.keys), 1)


class StaysInPlaceTests(unittest.TestCase):
    def test_camera_stays_put_when_the_car_moves(self):
        car0 = car_at(0.0, 0.0, 0.0)
        e = S.SequencerEngine()
        e.car_pose = lambda: car0
        key = {"pos": (7.0, 1.2, 1.0), "quat": q(5, -90), "fov": 70.0, "car": car0.to_dict()}
        e.key_at_playhead(key)
        e.scrub(0.0)
        w0, look0 = world_of(car0, *(lambda s: (s["pos"], s["quat"]))(e.sample()))
        car1 = car_at(12.0, 30.0, 25.0)
        e.car_pose = lambda: car1
        e.scrub(0.0)
        out = e.sample()
        w1, look1 = world_of(car1, out["pos"], out["quat"])
        self.assertLess(math.dist(w0, w1), 1e-6)
        self.assertLess(math.dist(look0, look1), 1e-6)
        self.assertNotAlmostEqual(out["pos"][2], 1.0)

    def test_follows_the_car_mode_ignores_the_move(self):
        car0, car1 = car_at(0, 0, 0), car_at(12, 30, 25)
        e = S.SequencerEngine()
        e.set_shot_field("mode", "car")
        e.car_pose = lambda: car0
        e.key_at_playhead({"pos": (7.0, 1.2, 1.0), "quat": q(), "fov": 70.0, "car": car0.to_dict()})
        e.car_pose = lambda: car1
        e.scrub(0.0)
        self.assertEqual(e.sample()["pos"], (7.0, 1.2, 1.0))

    def test_interpolates_in_the_world(self):
        car0, car1 = car_at(0, 0, 0), car_at(0, 20, 90)
        e = S.SequencerEngine()
        e.shot.ease_in = e.shot.ease_out = 0.0
        e.shot.smooth = False
        e.car_pose = lambda: car0
        e.key_at_playhead({"pos": (0, 1, 0), "quat": q(), "fov": 70, "car": car0.to_dict()})
        e.car_pose = lambda: car1
        e.key_at_playhead({"pos": (0, 1, 0), "quat": q(), "fov": 70, "car": car1.to_dict()})
        e.car_pose = lambda: car0
        e.play(now=0.0)
        out = e.sample(now=1.0)
        w, _ = world_of(car0, out["pos"], out["quat"])
        self.assertLess(math.dist(w, (0.0, 1.0, 10.0)), 1e-6)


class CabinTargetTests(unittest.TestCase):
    def test_inside_uses_the_driver_camera(self):
        self.assertEqual(S.cabin_target((0.0, 0.0, 0.0)), "driver")
        self.assertEqual(S.cabin_target((0.7, 0.1, 0.5)), "driver")
        self.assertEqual(S.cabin_target((0.0, 1.4, -5.5)), "follow_low")
        self.assertEqual(S.cabin_target((4.0, 0.0, 0.0)), "follow_low")


class FlyStaysOnTests(unittest.TestCase):
    def test_scrubbing_keeps_flying_and_jumps_the_fly_camera(self):
        e = S.SequencerEngine()
        e.key_at_playhead({"pos": (0, 1, -5), "quat": q(), "fov": 70})
        e.key_at_playhead({"pos": (8, 1, -5), "quat": q(), "fov": 70})
        e.fly({"space": "car", "pos": (3, 3, 3), "quat": q(), "fov": 70})
        e.scrub(2.0)
        self.assertEqual(e.state, "flying")
        self.assertIsNotNone(e.fly_jump)
        self.assertAlmostEqual(e.fly_jump["pos"][0], 8.0)
        self.assertEqual(e.sample()["pos"], (3, 3, 3))

    def test_key_while_flying_keeps_flying(self):
        e = S.SequencerEngine()
        e.fly({"space": "car", "pos": (3, 3, 3), "quat": q(), "fov": 70})
        e.key_at_playhead({"pos": (3, 3, 3), "quat": q(), "fov": 70})
        self.assertEqual(e.state, "flying")

    def test_flycam_moves_in_car_space(self):
        from horizonlens import overlay
        f = overlay.FlyCam()
        f.start_from({"pos": (0.0, 1.0, 0.0), "quat": q(), "fov": 70.0})
        f.speed = 2.0
        for _ in range(240):
            f.step(1 / 120, {"FWD": True})
        self.assertGreater(f.pos[2], 3.0)
        self.assertAlmostEqual(f.pos[0], 0.0, places=6)
        f.start_from({"pos": (0.0, 1.0, 0.0), "quat": q(0.0, 90.0), "fov": 70.0})
        self.assertAlmostEqual(f.yaw, 90.0, places=4)
        out = f.sample()
        self.assertEqual(out["space"], "car")
        self.assertLess(math.dist(relay_rotate(out["quat"], (0, 0, 1)), (1, 0, 0)), 1e-6)


class WireTests(unittest.TestCase):
    def test_sequencer_sends_exact_car_space(self):
        from horizonlens import server
        srv = server.HorizonServer(None, ("127.0.0.1", 8989))
        sent = []

        class Link:
            def send_pose(self, p):
                sent.append(p)
        srv.link = Link()
        srv.sequencer = e = S.SequencerEngine()
        e.key_at_playhead({"pos": (2.5, 1.5, -6.0), "quat": q(10, 20), "fov": 50.0})
        e.scrub(0.0)
        srv._send_sequencer_pose(0.0)
        p = sent[-1]
        self.assertEqual((p.kind, p.mode, p.options), (1, 0, 0))
        self.assertEqual(p.xyz, (2.5, 1.5, -6.0))
        self.assertAlmostEqual(p.fov, 50.0)
        self.assertEqual(srv.camera_target, "follow_low")

    def test_release_is_always_soft(self):
        import inspect
        from horizonlens import server
        src = inspect.getsource(server) + inspect.getsource(core)
        self.assertNotIn("Pose(kind=2)", src)
        self.assertNotIn("else 2", src)


class CalibrationFirstTests(unittest.TestCase):
    def test_switcher_shows_the_driver_camera_until_calibrated(self):
        class Srv:
            camera_target = "follow_low"
            _clients = {1}
            sequencer = None
            status = {"exterior_calibrated": False, "active_camera": "follow_low",
                      "active_camera_index": 0, "native_camera_selection_supported": True}

            def fresh_status(self):
                return self.status
        sent = []

        class Link:
            def select_camera(self, mode):
                sent.append(mode)

            def restore_camera(self):
                pass

        class Orch:
            server, link = Srv(), Link()
        sw = core.CameraSwitcher(Orch())
        sw._tick()
        self.assertEqual(sw.desired, "driver")
        self.assertEqual(sent, [core.CAMERA_COMMAND_MODES["driver"]])
        Orch.server.status = dict(Srv.status, exterior_calibrated=True)
        sw._tick()
        self.assertEqual(sw.desired, "follow_low")


if __name__ == "__main__":
    unittest.main()
