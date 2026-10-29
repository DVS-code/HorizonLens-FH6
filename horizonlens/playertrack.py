from __future__ import annotations

import math

from . import relay

RANGE_DEFAULT_M = 60.0
RANGE_MIN_M, RANGE_MAX_M = 5.0, 200.0
MOVING_MPS = 2.0
SWITCH_RATIO = 0.55
LOST_GRACE_S = 1.5
BLEND_S = 0.6
AIM_HEIGHT_M = 0.5
MAX_PITCH_DEG = 40.0
SPEED_SAMPLE_S = 0.15
SPEED_TIME_CONSTANT = 0.5
SMOOTH_SETTLE_S = {"low": 0.45, "medium": 0.9, "high": 1.6}
SMOOTH_DEFAULT = "medium"


def keep_range(range_m):
    return range_m * 1.35 + 5.0


def look_angles(direction):
    dx, dy, dz = direction
    flat = math.hypot(dx, dz)
    if flat < 1e-6 and abs(dy) < 1e-6:
        return None
    pitch = math.degrees(math.atan2(-dy, flat))
    return math.degrees(math.atan2(dx, dz)), max(-MAX_PITCH_DEG, min(MAX_PITCH_DEG, pitch))


def look_at(direction):
    a = look_angles(direction)
    return relay.q_from_euler_yxz_deg(a[1], a[0], 0.0) if a else None


def quat_angles(q):
    x, y, z, w = q
    return look_angles((2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)))


def pretty_name(media):
    parts = [p for p in (media or "").split("_") if p]
    if len(parts) >= 3 and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(parts[:3]) or "car"


class Spring:

    def __init__(self, value=0.0):
        self.value, self.rate = value, 0.0

    def update(self, target, dt, settle, wrap=False):
        if dt <= 0.0:
            return self.value
        err = target - self.value
        if wrap:
            err = math.remainder(err, 360.0)
        omega = 3.9 / max(0.05, settle)
        e = math.exp(-omega * dt)
        x0, v0 = -err, self.rate
        x1 = (x0 + (v0 + omega * x0) * dt) * e
        self.rate = (v0 - omega * (v0 + omega * x0) * dt) * e
        self.value = self.value + (x1 - x0)
        if wrap:
            self.value = math.remainder(self.value, 360.0)
        return self.value


class PlayerTrack:
    def __init__(self):
        self.range_m = RANGE_DEFAULT_M
        self.settle = SMOOTH_SETTLE_S[SMOOTH_DEFAULT]
        self.reset()

    def reset(self):
        self.target = None
        self.lost_since = None
        self.weight = 0.0
        self.yaw = self.pitch = None
        self.updated_at = None
        self.info = None
        self._motion = {}

    def configure(self, range_m=None, smooth=None):
        if isinstance(range_m, (int, float)) and math.isfinite(range_m):
            self.range_m = max(RANGE_MIN_M, min(RANGE_MAX_M, float(range_m)))
        if smooth in SMOOTH_SETTLE_S:
            self.settle = SMOOTH_SETTLE_S[smooth]

    def _measure(self, cars, now):
        seen = {}
        for c in cars:
            m = self._motion.get(c.entity)
            if m is None:
                seen[c.entity] = [now, c.pos, (0.0, 0.0, 0.0)]
                continue
            dt = now - m[0]
            if dt >= SPEED_SAMPLE_S:
                v = tuple((p - q) / dt for p, q in zip(c.pos, m[1]))
                if math.hypot(*v) > 150.0:
                    v = m[2]
                a = 1.0 - math.exp(-dt / SPEED_TIME_CONSTANT)
                m = [now, c.pos, tuple(o + (n - o) * a for o, n in zip(m[2], v))]
            seen[c.entity] = m
        self._motion = seen

    def _speed(self, entity):
        m = self._motion.get(entity)
        return math.hypot(*m[2]) if m else 0.0

    def _pick(self, me, cars, now):
        dist = {c.entity: math.dist(c.pos, me.pos) for c in cars}
        moving = {c.entity for c in cars if not c.traffic and self._speed(c.entity) >= MOVING_MPS}
        candidates = [c for c in cars if c.entity in moving and dist[c.entity] <= self.range_m]
        best = min(candidates, key=lambda c: dist[c.entity], default=None)
        current = next((c for c in cars if c.entity == self.target), None)
        if current is not None and dist[current.entity] <= keep_range(self.range_m):
            if current.entity in moving:
                self.lost_since = None
            elif self.lost_since is None:
                self.lost_since = now
            if self.lost_since is None or now - self.lost_since <= LOST_GRACE_S:
                if best is not None and best.entity != current.entity \
                        and dist[best.entity] < SWITCH_RATIO * dist[current.entity]:
                    current = best
                    self.lost_since = None
                self.target = current.entity
                return current, dist[current.entity]
        self.lost_since = None
        self.target = best.entity if best is not None else None
        return (best, dist[best.entity]) if best is not None else (None, None)

    def update(self, now, base_quat, cam_xyz, me, cars):
        dt = 0.0 if self.updated_at is None else max(0.0, min(0.1, now - self.updated_at))
        self.updated_at = now
        target, distance = None, None
        if me is not None:
            self._measure(cars, now)
            target, distance = self._pick(me, cars, now)
        else:
            self.target = None
        base = quat_angles(base_quat) or (0.0, 0.0)
        aim = None
        if target is not None:
            point = tuple(p + u * AIM_HEIGHT_M for p, u in zip(target.pos, me.axes[1]))
            local = me.point_to_car(point)
            aim = look_angles(tuple(a - b for a, b in zip(local, cam_xyz)))
        if self.yaw is None:
            self.yaw, self.pitch = Spring(base[0]), Spring(base[1])
        goal = aim or base
        self.yaw.update(goal[0], dt, self.settle, wrap=True)
        self.pitch.update(goal[1], dt, self.settle)
        step = dt / BLEND_S if dt else 0.0
        if aim is not None:
            self.weight = min(1.0, self.weight + step)
            self.info = {"name": pretty_name(target.name), "m": round(distance)}
        else:
            self.info = None
            back = abs(math.remainder(self.yaw.value - base[0], 360.0)) + abs(self.pitch.value - base[1])
            if back < 3.0:
                self.weight = max(0.0, self.weight - step)
        if self.weight <= 0.0:
            self.yaw = self.pitch = None
            return base_quat
        look = relay.q_from_euler_yxz_deg(self.pitch.value, self.yaw.value, 0.0)
        w = self.weight * self.weight * (3.0 - 2.0 * self.weight)
        return relay.q_norm(relay.q_slerp(base_quat, look, w))
