import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from horizonlens import carpose, playertrack, relay, server

IDENT = (0.0, 0.0, 0.0, 1.0)
DT = 1 / 90
CAM = (0.0, 1.0, -4.0)
AXES = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]


def forward(q):
    x, y, z, w = q
    return (2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y))


def angle(q, direction):
    f = forward(q)
    n = math.sqrt(sum(v * v for v in direction))
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b / n for a, b in zip(f, direction))))))


def q_angle(a, b):
    return math.degrees(2 * math.acos(min(1.0, abs(sum(x * y for x, y in zip(a, b))))))


def toward(world_pos, me):
    local = me.point_to_car((world_pos[0], world_pos[1] + playertrack.AIM_HEIGHT_M, world_pos[2]))
    return tuple(a - b for a, b in zip(local, CAM))


class World:

    def __init__(self, *cars, me_vel=(0.0, 0.0, 0.0)):
        self.cars = [dict(c) for c in cars]
        self.me_vel = me_vel

    def me(self, t):
        return carpose.CarPose(tuple(v * t for v in self.me_vel), AXES)

    def at(self, t):
        return [carpose.OtherCar(c["e"], c["name"],
                                 tuple(p + v * t for p, v in zip(c["pos"], c.get("vel", (0, 0, 0)))), 0.0)
                for c in self.cars]


def run(track, world, seconds, t0=0.0, base=IDENT, every=None):
    t, q, prev = t0, base, None
    for _ in range(int(seconds * 90)):
        t += DT
        q = track.update(t, base, CAM, world.me(t), world.at(t))
        if every and prev is not None:
            every(t, prev, q)
        prev = q
    return t, q


class LookTests(unittest.TestCase):
    def test_faces_the_direction(self):
        for d in ((0, 0, 1), (1, 0, 1), (-3, 0, 1), (0, -0.5, 1), (2, 1, -1)):
            self.assertLess(angle(playertrack.look_at(d), d), 0.01)
            yaw, pitch = playertrack.quat_angles(playertrack.look_at(d))
            self.assertLess(angle(relay.q_from_euler_yxz_deg(pitch, yaw, 0), d), 0.01)

    def test_steep_pitch_is_capped(self):
        q = playertrack.look_at((0, -10, 1))
        self.assertAlmostEqual(math.degrees(math.asin(-forward(q)[1])), playertrack.MAX_PITCH_DEG, places=3)

    def test_pretty_name(self):
        self.assertEqual(playertrack.pretty_name("BMW_M4_21"), "BMW M4")
        self.assertEqual(playertrack.pretty_name("MIT_LancerEvoVIIIMR_04"), "MIT LancerEvoVIIIMR")

    def test_spring_settles_without_overshoot(self):
        s, worst = playertrack.Spring(0.0), 0.0
        for _ in range(3 * 90):
            worst = max(worst, s.update(40.0, DT, 0.9))
        self.assertLessEqual(worst, 40.0 + 1e-9)
        self.assertAlmostEqual(s.value, 40.0, places=2)
        w = playertrack.Spring(170.0)
        for _ in range(3 * 90):
            w.update(-170.0, DT, 0.9, wrap=True)
        self.assertAlmostEqual(abs(w.value), 170.0, places=1)


class TrackTests(unittest.TestCase):
    def test_ignores_traffic_and_parked_cars(self):
        world = World({"e": 1, "name": "HON_e_Traffic_22", "pos": (5, 0, 20), "vel": (0, 0, 15)},
                      {"e": 2, "name": "KOE_One_15", "pos": (-5, 0, 15)})
        track = playertrack.PlayerTrack()
        _, q = run(track, world, 3)
        self.assertEqual(q, IDENT)
        self.assertIsNone(track.info)

    def test_racing_side_by_side_is_rock_steady(self):
        v = (0.0, 0.0, 60.0)
        world = World({"e": 7, "name": "BMW_M4_21", "pos": (4, 0, 6), "vel": v}, me_vel=v)
        track = playertrack.PlayerTrack()
        t, _ = run(track, world, 3)
        steps = []
        t, q = run(track, world, 2, t0=t, every=lambda t, a, b: steps.append(q_angle(a, b)))
        self.assertLess(max(steps), 0.01)
        self.assertLess(angle(q, toward(world.at(t)[0].pos, world.me(t))), 0.5)

    def test_follows_a_crossing_player_smoothly(self):
        world = World({"e": 7, "name": "BMW_M4_21", "pos": (30, 0, 40), "vel": (-10, 0, 0)})
        track = playertrack.PlayerTrack()
        steps = []
        t, q = run(track, world, 4, every=lambda t, a, b: steps.append(q_angle(a, b)))
        self.assertLess(max(steps), 0.8)
        self.assertLess(angle(q, toward(world.at(t)[0].pos, world.me(t))), 8.0)
        self.assertEqual(track.info["name"], "BMW M4")

    def test_range_setting(self):
        near = {"e": 7, "name": "BMW_M4_21", "pos": (3, 0, 8), "vel": (5, 0, 0)}
        track = playertrack.PlayerTrack()
        track.configure(range_m=10)
        run(track, World(near), 1.0)
        self.assertEqual(track.target, 7)
        far = {"e": 8, "name": "AUD_R8Plus_13", "pos": (0, 0, 30), "vel": (5, 0, 0)}
        track = playertrack.PlayerTrack()
        track.configure(range_m=10)
        _, q = run(track, World(far), 2.0)
        self.assertIsNone(track.target)
        self.assertEqual(q, IDENT)
        track.configure(range_m=1000, smooth="nonsense")
        self.assertEqual(track.range_m, playertrack.RANGE_MAX_M)
        self.assertEqual(track.settle, playertrack.SMOOTH_SETTLE_S["medium"])

    def test_eases_back_when_the_car_stops(self):
        world = World({"e": 7, "name": "BMW_M4_21", "pos": (40, 0, 10), "vel": (0, 0, 12)})
        track = playertrack.PlayerTrack()
        t, q = run(track, world, 3)
        self.assertGreater(angle(q, (0, 0, 1)), 20)
        stopped = World({"e": 7, "name": "BMW_M4_21", "pos": world.at(t)[0].pos})
        t, q = run(track, stopped, 1.0, t0=t)
        self.assertGreater(angle(q, (0, 0, 1)), 20)
        steps = []
        t, q = run(track, stopped, 5.0, t0=t, every=lambda t, a, b: steps.append(q_angle(a, b)))
        self.assertEqual(q, IDENT)
        self.assertLess(max(steps), 0.8)

    def test_sticks_with_its_car_unless_another_is_much_closer(self):
        a = {"e": 1, "name": "BMW_M4_21", "pos": (0, 0, 50), "vel": (5, 0, 0)}
        b = {"e": 2, "name": "AUD_R8Plus_13", "pos": (0, 0, 40), "vel": (5, 0, 0)}
        track = playertrack.PlayerTrack()
        run(track, World(a), 1.0)
        self.assertEqual(track.target, 1)
        run(track, World(a, b), 1.0, t0=1.0)
        self.assertEqual(track.target, 1)
        b["pos"] = (0, 0, 15)
        run(track, World(a, b), 1.0, t0=2.0)
        self.assertEqual(track.target, 2)


class ZoomCurveTests(unittest.TestCase):
    def test_defaults_match_the_original_curve(self):
        for mph in (0, 100, 150, 200, 230, 250, 300):
            self.assertAlmostEqual(relay.gap_speed_curve(mph), relay.gap_speed_factor(mph))

    def test_start_and_depth_settings(self):
        self.assertEqual(relay.gap_speed_curve(60, start=60), 1.0)
        self.assertLess(relay.gap_speed_curve(100, start=60), 1.0)
        self.assertAlmostEqual(relay.gap_speed_curve(250, start=60, max_out=0.6), 0.6)
        prev = 1.0
        for mph in range(60, 260):
            v = relay.gap_speed_curve(mph, start=60, max_out=0.6)
            self.assertLessEqual(v, prev + 1e-12)
            prev = v


class CarListTests(unittest.TestCase):

    def make(self, at):
        import struct
        mem_map = {}
        me, others = 0x1000, [0x2000, 0x3000, 0x4000]
        lst, arr, single = 0x9000, 0xA000, 0xB000
        obj = bytearray(carpose.LIST_SEARCH_BYTES)
        struct.pack_into("<QQ", obj, 0x18, single, single + 8)
        struct.pack_into("<QQ", obj, at, arr, arr + 8 * 4)
        mem_map[lst] = bytes(obj)
        mem_map[single] = struct.pack("<Q", me)
        mem_map[arr] = struct.pack("<4Q", me, *others)

        class Mem:
            def read(self, addr, size):
                for base, data in mem_map.items():
                    if base <= addr and addr + size <= base + len(data):
                        return data[addr - base:addr - base + size]
                return None

            def pointer(self, addr):
                d = self.read(addr, 8)
                return struct.unpack("<Q", d)[0] if d else None
        t = carpose.CarTracker()
        t._mem, t._entity = Mem(), me
        return t, lst

    def test_finds_the_list_wherever_it_moved(self):
        for at in (0x0, 0x1E0, 0x270, 0x3F0):
            t, lst = self.make(at)
            self.assertEqual(t._cars_in(lst), [0x1000, 0x2000, 0x3000, 0x4000])
            self.assertEqual(t._list_at[1], at)
            self.assertEqual(t._cars_in(lst), [0x1000, 0x2000, 0x3000, 0x4000])

    def test_nothing_without_our_car(self):
        t, lst = self.make(0x40)
        t._entity = 0x5555
        self.assertEqual(t._cars_in(lst), [])

class ServerTests(unittest.TestCase):
    def test_turns_the_gap_camera_only_when_asked(self):
        srv = server.HorizonServer(None, ("127.0.0.1", 8989))
        sent = []

        class Link:
            def send_pose(self, p):
                sent.append(p)
        srv.link = Link()
        srv._framing.path = None
        srv._framing.selected = relay.FRAME_GAP
        world = World({"e": 7, "name": "BMW_M4_21", "pos": (40, 0, 10), "vel": (0, 0, 12)})
        clock = {"t": 0.0}
        srv.pose_source = lambda: world.me(clock["t"])
        srv.cars_source = lambda: world.at(clock["t"])

        def tick(track, range_m=60):
            clock["t"] += DT
            sample = {"euler": (0.0, 0.0, 0.0), "zoom": 0.2, "fov_base": 70.0, "position": (0.0, 0.0, 0.0),
                      "gap_track": track, "gap_track_range": range_m, "gap_track_smooth": "low",
                      "tracking": True, "freecam": False}
            srv._send_pose(sample, clock["t"], clock["t"], True)
            return sent[-1]

        for _ in range(90):
            p = tick(False)
        still = p.quat
        self.assertIsNone(srv._gap_health())
        for _ in range(90):
            p = tick(True, range_m=10)
        self.assertEqual(srv._gap_health()["track"], {"name": None, "range": 10})
        for _ in range(3 * 90):
            p = tick(True)
        self.assertLess(angle(p.quat, (1, 0, 0)), angle(still, (1, 0, 0)) - 20)
        self.assertEqual(srv._gap_health()["track"]["name"], "BMW M4")


if __name__ == "__main__":
    unittest.main()
