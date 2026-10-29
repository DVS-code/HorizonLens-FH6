from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import carpose, config, curves, rcf6, relay

MODES = ("world", "car")
MAX_KEYS = 256
MAX_TRIGGERS = 256
DEFAULT_SPACING = 2.0
FLY_SECONDS = 0.8
MIN_SHOT = 0.1
ZOOM_MIN, ZOOM_MAX = 0.5, 3.0
TRACK_ZOOM, TRACK_AIR = "zoom", "air"
SPECIAL_TRACKS = (TRACK_ZOOM, TRACK_AIR)
WHEELS = 4
FRONT_WHEELS = (0, 1)
AXLE_OVERLAP = 0.7


def default_settings():
    return {
        "zoom": {"enabled": True, "transition": 1.0, "curve": list(curves.DEFAULT_CURVE)},
        "air": {"enabled": True, "transition": 2.5, "curve": list(curves.DEFAULT_CURVE),
                "drop": 60.0, "front": 0.0, "rear": 0.0, "floor": 15.0,
                "sequence": "together", "camber": False, "camber_front": -6.0,
                "camber_rear": -6.0, "camber_curve": list(curves.DEFAULT_CURVE),
                "return_on_stop": True},
        "animations": {"enabled": True, "reset_on_clean_play": False},
    }


SETTING_RULES = {
    ("zoom", "enabled"): "bool", ("zoom", "transition"): (0.0, 10.0), ("zoom", "curve"): "curve",
    ("air", "enabled"): "bool", ("air", "transition"): (0.0, 10.0), ("air", "curve"): "curve",
    ("air", "drop"): (0.0, 90.0), ("air", "front"): (-90.0, 50.0), ("air", "rear"): (-90.0, 50.0),
    ("air", "floor"): (0.0, 90.0), ("air", "sequence"): ("together", "front", "rear"),
    ("air", "camber"): "bool", ("air", "camber_front"): (-20.0, 20.0),
    ("air", "camber_rear"): (-20.0, 20.0), ("air", "camber_curve"): "curve",
    ("air", "return_on_stop"): "bool",
    ("animations", "enabled"): "bool", ("animations", "reset_on_clean_play"): "bool",
}


def clean_setting(module, key, value, fallback):
    rule = SETTING_RULES.get((module, key))
    if rule is None:
        return fallback
    if rule == "bool":
        return bool(value)
    if rule == "curve":
        return curves.normalise(value) if isinstance(value, (list, tuple)) else fallback
    if isinstance(rule, tuple) and rule and isinstance(rule[0], str):
        return value if value in rule else fallback
    return _num(value, fallback, rule[0], rule[1])


def clean_settings(raw):
    out = default_settings()
    raw = raw if isinstance(raw, dict) else {}
    for module, values in out.items():
        given = raw.get(module) if isinstance(raw.get(module), dict) else {}
        for key, default in values.items():
            if key in given:
                values[key] = clean_setting(module, key, given[key], default)
    return out


def axle_progress(p, wheel, order, reverse=False):
    if order == "together":
        return max(0.0, min(1.0, p))
    front_leads = (order == "front") != reverse
    leads = (wheel in FRONT_WHEELS) == front_leads
    start = 0.0 if leads else 1.0 - AXLE_OVERLAP
    return max(0.0, min(1.0, (p - start) / AXLE_OVERLAP))


def _normalized_zoom(z):
    z = max(ZOOM_MIN, min(ZOOM_MAX, float(z)))
    return (z - 0.5) / 0.5 * 0.2 if z <= 1 else 0.2 + (z - 1) / 2 * 0.8


def zoom_to_fov(z, base=relay.NATIVE_FOV):
    return relay.forza_fov(_normalized_zoom(z), base=base)


def fov_to_zoom(fov, base=relay.NATIVE_FOV):
    fov = float(fov)
    lo, hi = ZOOM_MIN, ZOOM_MAX
    if fov >= zoom_to_fov(lo, base):
        return lo
    if fov <= zoom_to_fov(hi, base):
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if zoom_to_fov(mid, base) > fov:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def quat_to_euler_yxz_deg(q) -> tuple[float, float, float]:
    x, y, z, w = relay.q_norm(tuple(float(v) for v in q))
    r02 = 2.0 * (x * z + w * y)
    r10 = 2.0 * (x * y + w * z)
    r11 = 1.0 - 2.0 * (x * x + z * z)
    r12 = 2.0 * (y * z - w * x)
    r22 = 1.0 - 2.0 * (x * x + y * y)
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -r12))))
    yaw = math.degrees(math.atan2(r02, r22))
    roll = math.degrees(math.atan2(r10, r11))
    return pitch, yaw, roll


def euler_yxz_deg_to_quat(pitch, yaw, roll):
    return relay.q_norm(rcf6.euler_to_quaternion(float(pitch), float(yaw), float(roll)))


def slerp(a, b, t):
    return relay.q_slerp(a, b, max(0.0, min(1.0, t)))


def smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _catmull_rom_centripetal(p0, p1, p2, p3, t):
    def knot(a, b, ti):
        d = math.dist(a, b) ** 0.5
        return ti + (d if d > 1e-6 else 1e-6)
    t0 = 0.0
    t1 = knot(p0, p1, t0)
    t2 = knot(p1, p2, t1)
    t3 = knot(p2, p3, t2)
    u = t1 + (t2 - t1) * t

    def lerp(a, b, ta, tb):
        w = (u - ta) / (tb - ta)
        return [x + (y - x) * w for x, y in zip(a, b)]
    a1 = lerp(p0, p1, t0, t1)
    a2 = lerp(p1, p2, t1, t2)
    a3 = lerp(p2, p3, t2, t3)
    b1 = lerp(a1, a2, t0, t2)
    b2 = lerp(a2, a3, t1, t3)
    return tuple(lerp(b1, b2, t1, t2))


def ease_profile(u, ease_in, ease_out):
    u = max(0.0, min(1.0, u))
    a = max(0.0, min(0.5, ease_in))
    b = max(0.0, min(0.5, ease_out))
    if a + b <= 1e-9:
        return u
    v = 1.0 / (1.0 - a / 2.0 - b / 2.0)
    if u < a:
        return v * u * u / (2.0 * a)
    if u > 1.0 - b:
        return 1.0 - v * (1.0 - u) ** 2 / (2.0 * b)
    return v * (a / 2.0 + (u - a))


def _axes_quat(axes):
    from .camworld import mat_to_quat
    r, u, f = axes
    return mat_to_quat([[r[0], u[0], f[0]], [r[1], u[1], f[1]], [r[2], u[2], f[2]]])


def _q_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def cabin_target(pos):
    x, y, z = pos
    return "driver" if (abs(x) < 1.1 and -0.7 < y < 0.45 and -2.4 < z < 1.7) else "follow_low"


DEFAULT_POSE = {"pos": (0.0, 1.4, -5.5), "quat": rcf6.euler_to_quaternion(8.0, 0.0, 0.0),
                "fov": 70.0}


def _num(value, default, lo=-1e7, hi=1e7):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v)) if math.isfinite(v) else default


@dataclass
class Key:
    t: float = 0.0
    pos: tuple = (0.0, 0.0, 0.0)
    quat: tuple = (0.0, 0.0, 0.0, 1.0)
    fov: float = 70.0
    lin: bool = False
    car: dict | None = None

    @property
    def car_pose(self):
        return carpose.CarPose.from_dict(self.car) if self.car else None

    @property
    def ypr(self):
        pitch, yaw, roll = quat_to_euler_yxz_deg(self.quat)
        return yaw, pitch, roll

    def set_ypr(self, yaw, pitch, roll):
        self.quat = euler_yxz_deg_to_quat(pitch, yaw, roll)

    def to_dict(self):
        d = {"t": round(self.t, 4), "pos": [round(v, 4) for v in self.pos],
             "quat": [round(v, 6) for v in self.quat], "fov": round(self.fov, 3),
             "lin": self.lin}
        if self.car:
            d["car"] = self.car
        return d

    @classmethod
    def from_dict(cls, d):
        pos = tuple(_num(v, 0.0) for v in (d.get("pos") or [0, 0, 0])[:3])
        quat = tuple(_num(v, 0.0, -1, 1) for v in (d.get("quat") or [0, 0, 0, 1])[:4])
        if len(pos) != 3:
            pos = (0.0, 0.0, 0.0)
        if len(quat) != 4 or sum(v * v for v in quat) < 1e-6:
            quat = (0.0, 0.0, 0.0, 1.0)
        car = d.get("car")
        car = car if isinstance(car, dict) and carpose.CarPose.from_dict(car) else None
        return cls(t=_num(d.get("t"), 0.0, 0.0, 3600.0), pos=pos, quat=relay.q_norm(quat),
                   fov=_num(d.get("fov"), 70.0, 10.0, 120.0), lin=bool(d.get("lin", False)),
                   car=car)


@dataclass
class Shot:
    name: str = "Shot 1"
    mode: str = "world"
    duration: float = 4.0
    ease_in: float = 0.35
    ease_out: float = 0.35
    smooth: bool = True
    spacing: float = DEFAULT_SPACING
    camera_target: str = "follow_low"
    keys: list = field(default_factory=list)
    trim_in: float = 0.0

    def sort(self):
        self.keys.sort(key=lambda k: k.t)

    @property
    def trim_out(self):
        return self.trim_in + self.duration

    def in_window(self, key):
        return self.trim_in - 1e-6 <= key.t <= self.trim_out + 1e-6

    def include(self, t):
        if t < self.trim_in:
            self.duration += self.trim_in - t
            self.trim_in = max(0.0, t)
        if t > self.trim_out:
            self.duration = t - self.trim_in

    def fit_duration(self):
        if self.keys:
            self.include(self.keys[0].t)
            self.include(self.keys[-1].t)

    def sample_world(self, t, car_now):
        if car_now is None or not self.keys:
            return self.sample(t)
        world = []
        for k in self.keys:
            ref = k.car_pose or car_now
            world.append(Key(t=k.t, pos=ref.point_to_world(k.pos),
                             quat=relay.q_norm(relay.q_mul(_axes_quat(ref.axes), k.quat)),
                             fov=k.fov, lin=k.lin))
        got = Shot(duration=self.duration, ease_in=self.ease_in, ease_out=self.ease_out,
                   smooth=self.smooth, keys=world, trim_in=self.trim_in).sample(t)
        pos, quat, fov = got
        car_pos = car_now.point_to_car(pos)
        car_quat = relay.q_norm(relay.q_mul(_q_conj(_axes_quat(car_now.axes)), quat))
        return car_pos, car_quat, fov

    def sample(self, t):
        keys = self.keys
        if not keys:
            return None
        if len(keys) == 1:
            k = keys[0]
            return k.pos, k.quat, k.fov
        a0 = keys[0].t
        span = max(1e-6, keys[-1].t - a0)
        t = a0 + ease_profile((self.trim_in + t - a0) / span, self.ease_in, self.ease_out) * span
        if t <= keys[0].t:
            k = keys[0]
            return k.pos, k.quat, k.fov
        if t >= keys[-1].t:
            k = keys[-1]
            return k.pos, k.quat, k.fov
        i = max(j for j in range(len(keys) - 1) if keys[j].t <= t)
        a, b = keys[i], keys[i + 1]
        f = (t - a.t) / max(1e-6, b.t - a.t)
        if self.smooth and not a.lin:
            p0 = keys[i - 1].pos if i > 0 else a.pos
            p3 = keys[i + 2].pos if i + 2 < len(keys) else b.pos
            pos = _catmull_rom_centripetal(p0, a.pos, b.pos, p3, f)
        else:
            pos = tuple(x + (y - x) * f for x, y in zip(a.pos, b.pos))
        quat = slerp(a.quat, b.quat, f)
        fov = a.fov + (b.fov - a.fov) * (f if a.lin else smoothstep(f))
        return pos, quat, fov

    def retime_constant_speed(self):
        if len(self.keys) < 3:
            return
        dists = [0.0]
        for a, b in zip(self.keys, self.keys[1:]):
            dists.append(dists[-1] + math.dist(a.pos, b.pos))
        total = dists[-1]
        if total < 1e-6:
            return
        t0, t1 = self.keys[0].t, self.keys[-1].t
        for k, d in zip(self.keys, dists):
            k.t = t0 + (t1 - t0) * d / total

    def to_dict(self):
        return {"name": self.name, "mode": self.mode, "duration": round(self.duration, 4),
                "trim_in": round(self.trim_in, 4), "ease_in": self.ease_in,
                "ease_out": self.ease_out, "smooth": self.smooth,
                "spacing": self.spacing, "camera_target": self.camera_target,
                "keys": [k.to_dict() for k in self.keys]}

    @classmethod
    def from_dict(cls, d):
        shot = cls(name=str(d.get("name") or "Shot")[:60],
                   mode=d.get("mode") if d.get("mode") in MODES else "world",
                   duration=_num(d.get("duration"), 4.0, MIN_SHOT, 3600.0),
                   trim_in=_num(d.get("trim_in"), 0.0, 0.0, 3600.0),
                   ease_in=_num(d.get("ease_in"), 0.35, 0.0, 0.5),
                   ease_out=_num(d.get("ease_out"), 0.35, 0.0, 0.5),
                   smooth=bool(d.get("smooth", True)),
                   spacing=_num(d.get("spacing"), DEFAULT_SPACING, 0.1, 60.0),
                   camera_target=d.get("camera_target") if d.get("camera_target") in
                   ("follow_low", "driver") else "follow_low",
                   keys=[Key.from_dict(k) for k in (d.get("keys") or [])[:MAX_KEYS]
                         if isinstance(k, dict)])
        shot.sort()
        if "trim_in" not in d:
            shot.fit_duration()
        return shot


def _action_ids():
    from . import vista
    return vista.ACTION_IDS


def is_one_shot(action):
    from . import vista
    return action in vista.ONE_SHOT_ACTIONS


@dataclass
class Trigger:
    t: float = 0.0
    action: str = "togglehood"
    value: float | None = None

    def to_dict(self):
        d = {"t": round(self.t, 4), "action": self.action}
        if self.value is not None:
            d["value"] = round(float(self.value), 4)
        return d


def valid_track(action):
    return isinstance(action, str) and (action in SPECIAL_TRACKS or action in _action_ids())


@dataclass
class Sequence:
    name: str = "Untitled"
    loop: bool = False
    shots: list = field(default_factory=lambda: [Shot()])
    triggers: list = field(default_factory=list)
    settings: dict = field(default_factory=default_settings)

    def sort_triggers(self):
        self.triggers.sort(key=lambda g: g.t)

    def track(self, action):
        return [g for g in self.triggers if g.action == action]

    def sample_keys(self, t, car_now=None):
        i, start = self.locate(t)
        shot = self.shots[i]
        if shot.mode == "world" and car_now is not None:
            return shot.sample_world(t - start, car_now)
        return shot.sample(t - start)

    def pose_at(self, t, car_now=None):
        got = self.sample_keys(t, car_now)
        if got is None:
            return None
        pos, quat, fov = got
        zoom = self.zoom_at(t)
        if zoom is not None:
            fov = zoom_to_fov(zoom)
        return pos, quat, fov

    def zoom_at(self, t, base=None):
        cfg = self.settings["zoom"]
        keys = self.track(TRACK_ZOOM) if cfg["enabled"] else []
        if not keys or t < keys[0].t - 1e-9:
            return None
        if base is None:
            got = self.sample_keys(keys[0].t)
            base = fov_to_zoom(got[2]) if got else 1.0
        span, curve = cfg["transition"], cfg["curve"]
        v = base
        for i, g in enumerate(keys):
            if g.t > t + 1e-9:
                break
            nxt = keys[i + 1].t if i + 1 < len(keys) and keys[i + 1].t <= t else t
            p = 1.0 if span <= 0 else (nxt - g.t) / span
            target = g.value if g.value is not None else 1.0
            v = v + (target - v) * curves.value(curve, p)
        return max(ZOOM_MIN, min(ZOOM_MAX, v))

    def air_at(self, t):
        cfg = self.settings["air"]
        keys = self.track(TRACK_AIR) if cfg["enabled"] else []
        if not keys:
            return None
        span, order = cfg["transition"], cfg["sequence"]
        heights, cambers = [0.0] * WHEELS, [0.0] * WHEELS
        for i, g in enumerate(keys):
            if g.t > t + 1e-9:
                break
            down = i % 2 == 0
            target = 1.0 if down else 0.0
            nxt = keys[i + 1].t if i + 1 < len(keys) and keys[i + 1].t <= t else t
            p = 1.0 if span <= 0 else (nxt - g.t) / span
            for w in range(WHEELS):
                f = curves.value(cfg["curve"], axle_progress(p, w, order, reverse=not down))
                heights[w] += (target - heights[w]) * f
                cambers[w] += (target - cambers[w]) * curves.value(cfg["camber_curve"], p)
        return heights, (cambers if cfg["camber"] else None)

    def animation_state(self, t):
        state = {}
        if not self.settings["animations"]["enabled"]:
            return state
        for g in self.triggers:
            if g.action in SPECIAL_TRACKS or is_one_shot(g.action):
                continue
            state.setdefault(g.action, False)
            if g.t <= t + 1e-9:
                state[g.action] = not state[g.action]
        return state

    def one_shots_between(self, t0, t1):
        if not self.settings["animations"]["enabled"]:
            return []
        return [g.action for g in self.triggers
                if is_one_shot(g.action) and t0 < g.t <= t1 + 1e-9]

    @property
    def duration(self):
        return sum(s.duration for s in self.shots)

    def locate(self, t):
        start = 0.0
        for i, s in enumerate(self.shots):
            if t < start + s.duration or i == len(self.shots) - 1:
                return i, start
            start += s.duration
        return 0, 0.0

    def shot_start(self, index):
        return sum(s.duration for s in self.shots[:index])

    def to_dict(self):
        return {"version": 4, "name": self.name, "loop": self.loop,
                "shots": [s.to_dict() for s in self.shots],
                "triggers": [g.to_dict() for g in self.triggers],
                "settings": self.settings}

    @classmethod
    def from_dict(cls, d):
        shots = [Shot.from_dict(s) for s in (d.get("shots") or []) if isinstance(s, dict)]
        triggers = []
        for g in (d.get("triggers") or [])[:MAX_TRIGGERS]:
            if not (isinstance(g, dict) and valid_track(g.get("action"))):
                continue
            value = None
            if g["action"] == TRACK_ZOOM:
                value = _num(g.get("value"), 1.0, ZOOM_MIN, ZOOM_MAX)
            triggers.append(Trigger(t=_num(g.get("t"), 0.0, 0.0, 36000.0), action=g["action"],
                                    value=value))
        seq = cls(name=str(d.get("name") or "Untitled")[:60], loop=bool(d.get("loop", False)),
                  shots=shots or [Shot()], triggers=triggers,
                  settings=clean_settings(d.get("settings")))
        seq.sort_triggers()
        return seq


def sequences_dir() -> Path:
    d = config.data_dir() / "sequences"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name):
    keep = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name)
    return (keep.strip() or "Untitled")[:60] + ".json"


def _write(path: Path, seq: Sequence):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(seq.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_named(seq: Sequence) -> Path:
    path = sequences_dir() / _safe_filename(seq.name)
    _write(path, seq)
    return path


def list_named():
    out = []
    for p in sorted(sequences_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.name == "autosave.json":
            continue
        try:
            name = str(json.loads(p.read_text(encoding="utf-8")).get("name", p.stem))
        except (OSError, ValueError):
            name = p.stem
        out.append((name, p))
    return out


def load_path(path) -> Sequence:
    return Sequence.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def delete_named(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def save_autosave(seq: Sequence):
    try:
        _write(sequences_dir() / "autosave.json", seq)
    except OSError:
        pass


def load_autosave() -> Sequence | None:
    path = sequences_dir() / "autosave.json"
    try:
        return load_path(path) if path.is_file() else None
    except (OSError, ValueError):
        return None


class SequencerEngine:

    def __init__(self, on_change=None):
        self._lock = threading.RLock()
        self.seq = Sequence()
        self.on_change = on_change
        self.listeners = []
        self.state = "stopped"
        self.speed = 1.0
        self.shot_index = 0
        self.key_index = -1
        self.trigger_index = -1
        self.revision = 0
        self.car_pose = lambda: None
        self.fly_jump = None
        self._playhead = 0.0
        self._anchor = None
        self._fly = None
        self._last = None
        self._undo, self._redo = [], []
        self.message = ""
        self._held = None

    def _notify(self, edited=False):
        if edited:
            self.revision += 1
        if edited and self.on_change:
            try:
                self.on_change(self.seq)
            except Exception:
                pass
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def edit(self, fn, label=""):
        with self._lock:
            before = copy.deepcopy(self.seq)
            fn(self.seq)
            for s in self.seq.shots:
                s.sort()
            self.seq.sort_triggers()
            self._undo.append(before)
            del self._undo[:-100]
            self._redo.clear()
            self.shot_index = max(0, min(self.shot_index, len(self.seq.shots) - 1))
            if self.trigger_index >= len(self.seq.triggers):
                self.trigger_index = -1
            self.message = label
        self._notify(edited=True)

    def undo(self):
        with self._lock:
            if not self._undo:
                return
            self._redo.append(copy.deepcopy(self.seq))
            self.seq = self._undo.pop()
            self.shot_index = min(self.shot_index, len(self.seq.shots) - 1)
            self.key_index = self.trigger_index = -1
        self._notify(edited=True)

    def redo(self):
        with self._lock:
            if not self._redo:
                return
            self._undo.append(copy.deepcopy(self.seq))
            self.seq = self._redo.pop()
            self.shot_index = min(self.shot_index, len(self.seq.shots) - 1)
            self.key_index = self.trigger_index = -1
        self._notify(edited=True)

    def load(self, seq: Sequence):
        with self._lock:
            self.stop()
            self.seq = seq
            self.shot_index, self.key_index, self.trigger_index = 0, -1, -1
            self._undo.clear()
            self._redo.clear()
        self._notify(edited=True)

    @property
    def shot(self) -> Shot:
        return self.seq.shots[self.shot_index]

    def is_engaged(self):
        return self.state != "stopped"

    def time(self, now=None):
        with self._lock:
            if self.state != "playing" or not self._anchor:
                return self._playhead
            now = time.monotonic() if now is None else now
            t0, head = self._anchor
            t = head + (now - t0) * self.speed
            total = self.seq.duration
            if t >= total:
                if self.seq.loop and total > 0:
                    return t % total
                return total
            return t

    def play(self, now=None):
        with self._lock:
            now = time.monotonic() if now is None else now
            head = self.time(now)
            if head >= self.seq.duration - 1e-6:
                head = 0.0
            self._playhead = head
            self._anchor = (now, head)
            self._fly = None
            self.state = "playing"
            self._held = None
        self._notify()

    def pause(self, now=None):
        with self._lock:
            if self.state == "flying" and self._last:
                held = dict(self._last)
                if held.get("space") == "freecam":
                    held["move"] = (0.0, 0.0, 0.0)
                self._held = held
            if self.state == "playing":
                self._playhead = self.time(now)
            self._anchor = None
            self.state = "paused"
        self._notify()

    def toggle_play(self, now=None):
        (self.pause if self.state == "playing" else self.play)(now)

    def fly(self, sample):
        with self._lock:
            self.state = "flying"
            self._anchor = None
            self._fly = None
            self._held = None
            self._last = dict(sample)

    def current_pose(self):
        with self._lock:
            return dict(self._last) if self._last else None

    def stop(self):
        with self._lock:
            self.state = "stopped"
            self._anchor = None
            self._fly = None
            self._held = None
            self._last = None
        self._notify()

    def scrub(self, t, now=None):
        with self._lock:
            self._playhead = max(0.0, min(self.seq.duration, float(t)))
            self._held = None
            if self.state == "playing":
                self._anchor = (time.monotonic() if now is None else now, self._playhead)
            elif self.state == "flying":
                got = self.timeline_pose(self._playhead)
                if got is not None:
                    self.fly_jump = got
            else:
                self.state = "paused"
            self._fly = None
            i, _ = self.seq.locate(self._playhead)
            self.shot_index = i
        self._notify()

    def timeline_pose(self, t=None):
        with self._lock:
            t = self._playhead if t is None else t
            got = self.seq.pose_at(t, self._car())
            if got is None:
                return None
            i, _ = self.seq.locate(t)
            return self._make_sample(self.seq.shots[i], *got)

    def _car(self):
        try:
            return self.car_pose()
        except Exception:
            return None

    def set_speed(self, speed, now=None):
        with self._lock:
            if self.state == "playing":
                self._playhead = self.time(now)
                self._anchor = (time.monotonic() if now is None else now, self._playhead)
            self.speed = max(0.25, min(2.0, float(speed)))
        self._notify()

    def fly_to_key(self, index=None, now=None):
        with self._lock:
            shot = self.shot
            index = self.key_index if index is None else index
            if not (0 <= index < len(shot.keys)):
                return False
            key = shot.keys[index]
            if self.state == "flying":
                self._playhead = self.seq.shot_start(self.shot_index) + \
                    max(0.0, min(shot.duration, key.t - shot.trim_in))
                self.key_index = index
                self.fly_jump = self._key_sample(shot, key)
                self._notify()
                return True
            target = self._key_sample(shot, key)
            now = time.monotonic() if now is None else now
            start = self._last if (self._last and self._last["space"] == target["space"]) else None
            self._fly = (start, target, now) if start else None
            self._playhead = self.seq.shot_start(self.shot_index) + \
                max(0.0, min(shot.duration, key.t - shot.trim_in))
            self._anchor = None
            self.state = "paused"
            self._held = None if self._fly else target
            self.key_index = index
        self._notify()
        return True

    def _make_sample(self, shot, pos, quat, fov):
        return {"space": "car", "pos": tuple(pos), "quat": tuple(quat), "fov": float(fov),
                "camera_target": cabin_target(pos)}

    def _key_sample(self, shot, key):
        car_now = self._car() if shot.mode == "world" else None
        ref = key.car_pose
        if car_now is not None and ref is not None:
            pos = car_now.point_to_car(ref.point_to_world(key.pos))
            quat = relay.q_norm(relay.q_mul(_q_conj(_axes_quat(car_now.axes)),
                                            relay.q_mul(_axes_quat(ref.axes), key.quat)))
            return self._make_sample(shot, pos, quat, key.fov)
        return self._make_sample(shot, key.pos, key.quat, key.fov)

    def sample(self, now=None):
        with self._lock:
            if self.state == "stopped":
                return None
            if self.state == "flying":
                return self._last
            if self.state == "paused" and self._held is not None:
                return self._held
            now = time.monotonic() if now is None else now
            if self._fly:
                start, target, t0 = self._fly
                f = smoothstep((now - t0) / FLY_SECONDS)
                if f >= 1.0:
                    self._fly = None
                    self._last = target
                    self._held = target
                    return target
                out = dict(target, pos=tuple(a + (b - a) * f for a, b in zip(start["pos"], target["pos"])),
                           quat=slerp(start["quat"], target["quat"], f),
                           fov=start["fov"] + (target["fov"] - start["fov"]) * f)
                self._last = out
                return out
            t = self.time(now)
            if self.state == "playing" and not self.seq.loop and t >= self.seq.duration:
                self._playhead = self.seq.duration
                self.state = "paused"
                self._anchor = None
            i, _ = self.seq.locate(t)
            shot = self.seq.shots[i]
            got = self.seq.pose_at(t, self._car() if shot.mode == "world" else None)
            if got is None:
                return self._last
            self._last = self._make_sample(shot, *got)
            return self._last

    def snapshot(self):
        with self._lock:
            return {"state": self.state, "time": round(self.time(), 3),
                    "duration": round(self.seq.duration, 3), "shot": self.shot_index,
                    "shots": len(self.seq.shots), "keys": len(self.shot.keys),
                    "mode": self.shot.mode, "name": self.seq.name}

    def key_at_playhead(self, pose):
        with self._lock:
            t_seq = self._playhead
            i, start = self.seq.locate(t_seq)
            shot = self.seq.shots[i]
            t_key = max(0.0, shot.trim_in + (t_seq - start))
            existing = next((j for j, k in enumerate(shot.keys) if abs(k.t - t_key) < 0.05), None)
            self.shot_index = i
        if existing is not None:
            self.update_key(pose, existing)
            self.key_index = existing
            return "updated"
        car = pose.get("car")

        def fn(seq):
            s = seq.shots[i]
            if len(s.keys) >= MAX_KEYS:
                return
            s.keys.append(Key(t=t_key, pos=tuple(pose["pos"]), quat=tuple(pose["quat"]),
                              fov=float(pose["fov"]), car=car))
            s.sort()
            s.include(t_key)
            if t_key + s.spacing > s.trim_out:
                s.duration = t_key - s.trim_in + s.spacing
        self.edit(fn, "Key added")
        with self._lock:
            shot = self.seq.shots[i]
            self.key_index = next((j for j, k in enumerate(shot.keys) if abs(k.t - t_key) < 0.011), -1)
            self._playhead = self.seq.shot_start(i) + (t_key - shot.trim_in) + shot.spacing
            self._playhead = min(self._playhead, self.seq.duration)
            if self.state not in ("flying", "playing"):
                self.state = "paused"
                self._held = None
        self._notify()
        return "added"

    def add_key(self, pose, at=None):
        def fn(seq):
            shot = seq.shots[self.shot_index]
            if len(shot.keys) >= MAX_KEYS:
                return
            if at is not None:
                t = max(0.0, shot.trim_in + float(at))
            else:
                t = (shot.keys[-1].t + shot.spacing) if shot.keys else shot.trim_in
            for k in shot.keys:
                if abs(k.t - t) < 0.01:
                    k.pos, k.quat, k.fov = tuple(pose["pos"]), tuple(pose["quat"]), float(pose["fov"])
                    k.car = pose.get("car")
                    break
            else:
                shot.keys.append(Key(t=t, pos=tuple(pose["pos"]), quat=tuple(pose["quat"]),
                                     fov=float(pose["fov"]), car=pose.get("car")))
            shot.sort()
            shot.include(t)
            self.key_index = next(i for i, k in enumerate(shot.keys) if abs(k.t - t) < 0.011)
        self.edit(fn, "Key added")

    def update_key(self, pose, index=None):
        index = self.key_index if index is None else index
        if not (0 <= index < len(self.shot.keys)):
            return False

        def fn(seq):
            k = seq.shots[self.shot_index].keys[index]
            k.pos, k.quat, k.fov = tuple(pose["pos"]), tuple(pose["quat"]), float(pose["fov"])
            k.car = pose.get("car")
        self.edit(fn, "Key updated from the camera")
        return True

    def set_key_field(self, index, name, value):
        def fn(seq):
            shot = seq.shots[self.shot_index]
            k = shot.keys[index]
            if name == "t":
                k.t = max(0.0, shot.trim_in + float(value))
                shot.include(k.t)
            elif name in ("x", "y", "z"):
                p = list(k.pos)
                p["xyz".index(name)] = float(value)
                k.pos = tuple(p)
            elif name in ("yaw", "pitch", "roll"):
                yaw, pitch, roll = k.ypr
                vals = {"yaw": yaw, "pitch": pitch, "roll": roll}
                vals[name] = float(value)
                k.set_ypr(vals["yaw"], vals["pitch"], vals["roll"])
            elif name == "fov":
                k.fov = max(10.0, min(120.0, float(value)))
            elif name == "lin":
                k.lin = bool(value)
        self.edit(fn)

    def move_key(self, index, new_t):
        shot = self.shot
        if not (0 <= index < len(shot.keys)):
            return index
        key_id = id(shot.keys[index])

        def fn(seq):
            shot = seq.shots[self.shot_index]
            shot.keys[index].t = max(0.0, shot.trim_in + float(new_t))
            shot.include(shot.keys[index].t)
        self.edit(fn)
        for i, k in enumerate(self.shot.keys):
            if id(k) == key_id:
                self.key_index = i
                return i
        return index

    def delete_key(self, index=None):
        index = self.key_index if index is None else index
        if not (0 <= index < len(self.shot.keys)):
            return

        def fn(seq):
            del seq.shots[self.shot_index].keys[index]
        self.key_index = min(index, len(self.shot.keys) - 2)
        self.edit(fn, "Key deleted")

    def add_shot(self):
        def fn(seq):
            seq.shots.insert(self.shot_index + 1, Shot(name=f"Shot {len(seq.shots) + 1}",
                                                       mode=self.shot.mode))
        self.edit(fn, "Shot added")
        self.shot_index += 1
        self.key_index = -1
        self._notify()

    def duplicate_shot(self):
        def fn(seq):
            dup = copy.deepcopy(seq.shots[self.shot_index])
            dup.name = (dup.name + " copy")[:60]
            seq.shots.insert(self.shot_index + 1, dup)
        self.edit(fn, "Shot duplicated")
        self.shot_index += 1
        self._notify()

    def delete_shot(self):
        if len(self.seq.shots) <= 1:
            def fn(seq):
                seq.shots[0] = Shot()
        else:
            def fn(seq):
                del seq.shots[self.shot_index]
        self.edit(fn, "Shot deleted")
        self.key_index = -1

    def select_shot(self, index):
        with self._lock:
            self.shot_index = max(0, min(index, len(self.seq.shots) - 1))
            self.key_index = -1
            if self.state != "playing":
                self._playhead = self.seq.shot_start(self.shot_index)
        self._notify()

    def set_shot_field(self, name, value):
        def fn(seq):
            s = seq.shots[self.shot_index]
            if name == "name":
                s.name = str(value)[:60] or s.name
            elif name == "mode" and value in MODES and value != s.mode:
                s.mode = value
            elif name == "duration":
                s.duration = max(MIN_SHOT, float(value))
            elif name in ("ease_in", "ease_out"):
                setattr(s, name, max(0.0, min(0.5, float(value))))
            elif name == "spacing":
                s.spacing = max(0.1, float(value))
            elif name == "smooth":
                s.smooth = bool(value)
        self.edit(fn)

    def retime_constant_speed(self):
        self.edit(lambda seq: seq.shots[self.shot_index].retime_constant_speed(),
                  "Keys retimed for constant speed")

    def split_shot(self, t=None):
        with self._lock:
            t = self.time() if t is None else float(t)
            i, start = self.seq.locate(t)
            shot = self.seq.shots[i]
            cut = t - start
        if not (MIN_SHOT <= cut <= shot.duration - MIN_SHOT):
            self.message = "Put the playhead inside a shot to split it"
            self._notify()
            return False

        def fn(seq):
            a = seq.shots[i]
            b = copy.deepcopy(a)
            b.trim_in = a.trim_in + cut
            b.duration = a.duration - cut
            a.duration = cut
            b.name = _next_name(a.name)
            seq.shots.insert(i + 1, b)
        self.edit(fn, "Shot split")
        with self._lock:
            self.shot_index, self.key_index = i + 1, -1
        self._notify()
        return True

    def trim_shot(self, index, trim_in, duration):
        if not (0 <= index < len(self.seq.shots)):
            return

        def fn(seq):
            s = seq.shots[index]
            s.trim_in = max(0.0, float(trim_in))
            s.duration = max(MIN_SHOT, float(duration))
        self.edit(fn, "Shot trimmed")

    def move_shot(self, index, to):
        n = len(self.seq.shots)
        to = max(0, min(n - 1, int(to)))
        if not (0 <= index < n) or to == index:
            return

        def fn(seq):
            seq.shots.insert(to, seq.shots.pop(index))
        self.edit(fn, "Shot moved")
        with self._lock:
            self.shot_index, self.key_index = to, -1
            if self.state != "playing":
                self._playhead = self.seq.shot_start(to)
        self._notify()

    def add_trigger(self, action, t=None, value=None):
        if not valid_track(action) or len(self.seq.triggers) >= MAX_TRIGGERS:
            return
        t = self.time() if t is None else float(t)
        t = max(0.0, min(self.seq.duration, t))
        if action == TRACK_ZOOM:
            value = max(ZOOM_MIN, min(ZOOM_MAX, float(1.0 if value is None else value)))
        else:
            value = None
        new = Trigger(t=t, action=action, value=value)

        def fn(seq):
            seq.triggers.append(new)
        self.edit(fn, "Animation added")
        with self._lock:
            self.trigger_index = next(i for i, g in enumerate(self.seq.triggers) if g is new)
        self._notify()

    def move_trigger(self, index, t):
        if not (0 <= index < len(self.seq.triggers)):
            return
        moved = self.seq.triggers[index]

        def fn(seq):
            seq.triggers[index].t = max(0.0, min(seq.duration, float(t)))
        self.edit(fn)
        with self._lock:
            for i, g in enumerate(self.seq.triggers):
                if g is moved:
                    self.trigger_index = i
        self._notify()

    def delete_trigger(self, index=None):
        index = self.trigger_index if index is None else index
        if not (0 <= index < len(self.seq.triggers)):
            return

        def fn(seq):
            del seq.triggers[index]
        self.trigger_index = -1
        self.edit(fn, "Animation deleted")

    def set_trigger_value(self, index, value):
        if not (0 <= index < len(self.seq.triggers)) or \
                self.seq.triggers[index].action != TRACK_ZOOM:
            return

        def fn(seq):
            seq.triggers[index].value = max(ZOOM_MIN, min(ZOOM_MAX, float(value)))
        self.edit(fn, "Zoom keyframe set")

    def selected_trigger(self):
        i = self.trigger_index
        return self.seq.triggers[i] if 0 <= i < len(self.seq.triggers) else None

    def set_setting(self, module, key, value):
        if (module, key) not in SETTING_RULES:
            return

        def fn(seq):
            seq.settings[module][key] = clean_setting(module, key, value, seq.settings[module][key])
        self.edit(fn)

    def reset_settings(self, module=None):
        def fn(seq):
            fresh = default_settings()
            for m in ([module] if module else list(fresh)):
                seq.settings[m] = fresh[m]
        self.edit(fn, "Settings reset")

    def animation_state(self, t=None):
        with self._lock:
            return self.seq.animation_state(self.time() if t is None else t)

    def air_state(self, t=None):
        with self._lock:
            return self.seq.air_at(self.time() if t is None else t)

    def set_name(self, name):
        self.edit(lambda seq: setattr(seq, "name", (str(name).strip() or "Untitled")[:60]))

    def set_loop(self, loop):
        self.edit(lambda seq: setattr(seq, "loop", bool(loop)))


def _next_name(name):
    import re
    m = re.match(r"^(.*) \((\d+)\)$", name)
    if m:
        return f"{m.group(1)} ({int(m.group(2)) + 1})"[:60]
    return name[:55] + " (2)"
