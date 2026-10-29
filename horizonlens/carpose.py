from __future__ import annotations

import math
import struct
import threading
import time

from . import airride, game

ROT_OFFSET = 0x1590
POS_OFFSET = 0x15D0
SPEED_OFFSET = 0x14EC

LIST_SEARCH_BYTES = 0x400
MAX_CARS = 128


class OtherCar:
    __slots__ = ("entity", "name", "pos", "speed")

    def __init__(self, entity, name, pos, speed):
        self.entity, self.name, self.pos, self.speed = entity, name, tuple(pos), speed

    @property
    def traffic(self):
        return "_Traffic_" in (self.name or "")


def _orthonormal(cols):
    for c in cols:
        if abs(sum(v * v for v in c) - 1.0) > 0.03:
            return False
    for a, b in ((0, 1), (0, 2), (1, 2)):
        if abs(sum(x * y for x, y in zip(cols[a], cols[b]))) > 0.03:
            return False
    return True


class CarPose:

    def __init__(self, pos, axes):
        self.pos = tuple(pos)
        self.axes = [tuple(a) for a in axes]

    def to_world(self, v):
        r, u, f = self.axes
        return tuple(v[0] * r[i] + v[1] * u[i] + v[2] * f[i] for i in range(3))

    def to_car(self, w):
        return tuple(sum(w[i] * a[i] for i in range(3)) for a in self.axes)

    def point_to_world(self, p):
        d = self.to_world(p)
        return tuple(self.pos[i] + d[i] for i in range(3))

    def point_to_car(self, w):
        return self.to_car(tuple(w[i] - self.pos[i] for i in range(3)))

    def to_dict(self):
        return {"pos": [round(v, 4) for v in self.pos],
                "axes": [[round(v, 6) for v in a] for a in self.axes]}

    @classmethod
    def from_dict(cls, d):
        try:
            pos = [float(v) for v in d["pos"]][:3]
            axes = [[float(v) for v in a][:3] for a in d["axes"]][:3]
            if len(pos) == 3 and len(axes) == 3 and all(len(a) == 3 for a in axes) \
                    and all(math.isfinite(v) for v in pos) and _orthonormal(axes):
                return cls(pos, axes)
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def moved_from(self, other):
        dist = math.dist(self.pos, other.pos)
        dots = [max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
                for a, b in zip(self.axes, other.axes)]
        return dist, max(math.degrees(math.acos(d)) for d in dots)


class CarTracker:

    RESCAN_SECONDS = 2.0

    def __init__(self):
        self._lock = threading.Lock()
        self._pid = None
        self._mem = None
        self._context = None
        self._car = None
        self._next_scan = 0.0
        self._last = (0.0, None)
        self._entity = None
        self._names = {}
        self._last_others = (0.0, [], True)

    def _attach(self):
        pid = game.game_pid_cached(2.0)
        if not pid:
            self._close()
            return False
        if pid != self._pid or self._mem is None or not self._mem.alive:
            self._close()
            from .camera_scan import _main_module_base
            base, _ = _main_module_base(pid)
            if not base:
                return False
            try:
                self._mem = airride._Mem(pid, base)
            except OSError:
                return False
            self._pid = pid
            self._context = airride.resolve_context(self._mem)
        return True

    def _close(self):
        if self._mem is not None:
            self._mem.close()
        self._mem = self._car = self._entity = None
        self._pid = None
        self._names = {}

    def _read(self):
        if self._car is None:
            if time.monotonic() < self._next_scan:
                return None
            self._next_scan = time.monotonic() + self.RESCAN_SECONDS
            car, _ = airride.find_car(self._mem, self._context)
            if car is None:
                return None
            self._car, self._entity = car.car, car.entity
        rot =self._mem.read(self._car + ROT_OFFSET, 48)
        pos = self._mem.read(self._car + POS_OFFSET, 12)
        if not rot or not pos:
            self._car = None
            return None
        m = struct.unpack("<12f", rot)
        rows = [m[0:3], m[4:7], m[8:11]]
        cols = [(rows[0][j], rows[1][j], rows[2][j]) for j in range(3)]
        p = struct.unpack("<3f", pos)
        if not _orthonormal(cols) or not all(math.isfinite(v) and abs(v) < 1e6 for v in p):
            self._car = None
            return None
        return CarPose(p, cols)

    def pose(self, max_age=0.0):
        with self._lock:
            now = time.monotonic()
            if max_age and now - self._last[0] <= max_age:
                return self._last[1]
            try:
                got = self._read() if self._attach() else None
            except Exception:
                got = None
            self._last = (now, got)
            return got

    def speed(self, max_age=0.05):
        with self._lock:
            now = time.monotonic()
            last = getattr(self, "_last_speed", (0.0, None))
            if max_age and now - last[0] <= max_age:
                return last[1]
            got = None
            try:
                if self._attach():
                    if self._car is None:
                        self._read()
                    if self._car is not None:
                        raw = self._mem.read(self._car + SPEED_OFFSET, 4)
                        v = abs(struct.unpack("<f", raw)[0]) if raw else None
                        got = v if v is not None and math.isfinite(v) and v < 250.0 else None
            except Exception:
                got = None
            self._last_speed = (now, got)
            return got

    def _car_list(self):
        mem = self._mem
        a = mem.pointer(mem.base + self._context)
        b = mem.pointer(a + airride.CTX_TO_A) if a else None
        c = mem.pointer(b + airride.A_TO_MGR) if b else None
        lst = mem.pointer(c + airride.MGR_TO_LIST) if c else None
        return self._cars_in(lst) if lst else []

    def _cars_in(self, lst):
        mem = self._mem
        cached = getattr(self, "_list_at", None)
        if cached is not None and cached[0] == lst:
            got = self._vector(lst + cached[1])
            if got and self._entity in got and len(got) == cached[2]:
                return got
        raw = mem.read(lst, LIST_SEARCH_BYTES)
        best = None
        for off in range(0, len(raw or b"") - 8, 8) if raw else ():
            begin, end = struct.unpack_from("<QQ", raw, off)
            n = (end - begin) // 8 if begin and end > begin and not (end - begin) % 8 else 0
            if n < 2 or n > MAX_CARS or (best and n <= best[2]):
                continue
            got = self._vector(lst + off)
            if got and self._entity in got:
                best = (lst, off, len(got), got)
        if best is None:
            self._list_at = None
            return []
        self._list_at = best[:3]
        return best[3]

    def _vector(self, at):
        mem = self._mem
        raw = mem.read(at, 16)
        if not raw:
            return None
        begin, end = struct.unpack("<QQ", raw)
        if not begin or end <= begin or (end - begin) % 8 or (end - begin) // 8 > MAX_CARS:
            return None
        slots = mem.read(begin, end - begin)
        return [v for v in struct.unpack(f"<{len(slots) // 8}Q", slots) if v] if slots else None

    def others(self, max_age=0.1, traffic=True):
        with self._lock:
            now = time.monotonic()
            if max_age and now - self._last_others[0] <= max_age and self._last_others[2] == traffic:
                return self._last_others[1]
            got = []
            try:
                if self._attach():
                    if self._car is None:
                        self._read()
                    if self._entity is not None:
                        mem, names = self._mem, {}
                        for entity in self._car_list():
                            if entity == self._entity:
                                continue
                            car = mem.pointer(entity + airride.ENTITY_TO_CAR)
                            if not car:
                                continue
                            key = (entity, car)
                            name = self._names.get(key)
                            if name is None:
                                name = airride.Car(mem, entity, car).media_name or ""
                            names[key] = name
                            if not traffic and "_Traffic_" in name:
                                continue
                            pos = mem.read(car + POS_OFFSET, 12)
                            spd = mem.read(car + SPEED_OFFSET, 4)
                            if not name or not pos or not spd:
                                continue
                            p = struct.unpack("<3f", pos)
                            v = abs(struct.unpack("<f", spd)[0])
                            if all(math.isfinite(x) and abs(x) < 1e6 for x in p) and math.isfinite(v):
                                got.append(OtherCar(entity, name, p, min(v, 250.0)))
                        self._names = names
            except Exception:
                got = []
            self._last_others = (now, got, traffic)
            return got

    def close(self):
        with self._lock:
            self._close()
