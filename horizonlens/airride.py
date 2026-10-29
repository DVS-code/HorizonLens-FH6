from __future__ import annotations

import ctypes
import math
import re
import struct
import threading
import time
from ctypes import wintypes

from . import curves, game

CTX_FALLBACK = 0xA8CFEB8
CTX_SIG = "06 E8 ? ? ? ? 90 48 8D 4D C0 E8 ? ? ? ? 48 8B 15 ? ? ? ? 48 8B 82 A8 07 00 00"
CTX_SIG_INSN = 16
CTX_SIG_DISP = CTX_SIG_INSN + 3
CTX_SIG_INSN_LEN = 7
CTX_TO_A, A_TO_MGR, MGR_TO_LIST = 0x1D0, 0x128, 0x370
LIST_VEC_BEGIN, LIST_VEC_END = 0x18, 0x20
ENTITY_TO_CAR = 0x7740
CAR_ENGINE_RPM = 0x1B0
CAR_MAX_SPEED = 0x24C
RAD_TO_RPM = 9.549296

WHEELS_BASE, WHEEL_STRIDE, WHEEL_COUNT = 0x28A8, 0x0AC0, 4
RIDE_HEIGHT, RADIUS, CAMBER_SIN_LIVE = 0x02E0, 0x0234, 0x0120
FRONT_WHEELS = (0, 1)

TABLE_PTR, TABLE_SUB = 0x408, 0x30
TABLE_REGION = (0x20, 0xD40, 0xD40, 0x20)
TABLE_ENTRY_SIZE, TABLE_ENTRY_COUNT, TABLE_CAMBER_SIN = 112, 30, 0x18

ENTITY_TO_RECORD = 0x2F0

RAMP_HZ = 60
SETTLE_SECONDS = 0.5
HARD_FLOOR_M = 0.01
AXLE_OVERLAP = 0.7
CAMBER_MIN_DEG, CAMBER_MAX_DEG = -20.0, 20.0
LOWER_PERCENT_MAX, RAISE_PERCENT_MAX = 90.0, 50.0
RESCAN_SECONDS = 1.0
REBAKE_SECONDS = 0.1

CAMBER_CURVES = {name: curves.PRESETS[name] for name in ("smooth", "linear", "early", "late")}
curve_value = curves.value


DEFAULTS = {
    "enabled": False,
    "front": 0.0,
    "rear": 0.0,
    "drop": 60.0,
    "floor": 15.0,
    "speed": 2.5,
    "sequence": "together",
    "camber": False,
    "camber_front": -6.0,
    "camber_rear": -6.0,
    "curve": "smooth",
}


def clean_settings(raw: dict, base: dict | None = None) -> dict:
    out = dict(base or DEFAULTS)

    def num(key, lo, hi):
        try:
            v = float(raw[key])
        except (KeyError, TypeError, ValueError):
            return
        if math.isfinite(v):
            out[key] = max(lo, min(hi, v))

    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "camber" in raw:
        out["camber"] = bool(raw["camber"])
    num("front", -LOWER_PERCENT_MAX, RAISE_PERCENT_MAX)
    num("rear", -LOWER_PERCENT_MAX, RAISE_PERCENT_MAX)
    num("drop", 0.0, LOWER_PERCENT_MAX)
    num("floor", 0.0, LOWER_PERCENT_MAX)
    num("speed", 0.3, 8.0)
    num("camber_front", CAMBER_MIN_DEG, CAMBER_MAX_DEG)
    num("camber_rear", CAMBER_MIN_DEG, CAMBER_MAX_DEG)
    if raw.get("sequence") in ("together", "front", "rear"):
        out["sequence"] = raw["sequence"]
    if isinstance(raw.get("curve"), str) and raw["curve"] in CAMBER_CURVES:
        out["curve"] = raw["curve"]
    return out


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.ReadProcessMemory.restype = wintypes.BOOL
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.WriteProcessMemory.restype = wintypes.BOOL
_k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_ACCESS = 0x0400 | 0x0010 | 0x0020 | 0x0008


class _Mem:
    def __init__(self, pid: int, base: int):
        self.pid, self.base = pid, base
        self.h = _k32.OpenProcess(_ACCESS, False, pid)
        if not self.h:
            raise OSError(f"can't open the game for writing (error {ctypes.get_last_error()})")

    def close(self):
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None

    @property
    def alive(self) -> bool:
        code = wintypes.DWORD()
        return bool(self.h and _k32.GetExitCodeProcess(self.h, ctypes.byref(code))
                    and code.value == 259)

    def read(self, addr: int, size: int) -> bytes | None:
        if not addr or size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not _k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)) \
                or got.value != size:
            return None
        return buf.raw

    def write(self, addr: int, data: bytes) -> bool:
        if not addr or not data:
            return False
        done = ctypes.c_size_t()
        return bool(_k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), data, len(data),
                                            ctypes.byref(done))) and done.value == len(data)

    def f32(self, addr):
        d = self.read(addr, 4)
        return struct.unpack("<f", d)[0] if d else None

    def pointer(self, addr):
        d = self.read(addr, 8)
        return (struct.unpack("<Q", d)[0] or None) if d else None


def _image_size(mem: _Mem) -> int:
    head = mem.read(mem.base, 0x40)
    if not head or head[:2] != b"MZ":
        return 0
    pe = int.from_bytes(head[0x3C:0x40], "little")
    opt = mem.read(mem.base + pe + 0x18, 0x70)
    return int.from_bytes(opt[0x38:0x3C], "little") if opt else 0


def _compile(sig: str):
    return re.compile(b"".join(b"." if t in ("?", "??") else re.escape(bytes([int(t, 16)]))
                               for t in sig.split()), re.DOTALL)


def resolve_context(mem: _Mem) -> int:
    size = _image_size(mem)
    if size:
        chunk, overlap = 0x400000, 64
        pattern = _compile(CTX_SIG)
        hits = []
        for off in range(0, size, chunk):
            length = min(chunk + overlap, size - off)
            block = mem.read(mem.base + off, length)
            if not block:
                parts = []
                for sub in range(0, length, 0x10000):
                    n = min(0x10000, length - sub)
                    parts.append(mem.read(mem.base + off + sub, n) or b"\x00" * n)
                block = b"".join(parts)
            for m in pattern.finditer(block):
                if m.start() < chunk:
                    hits.append(mem.base + off + m.start())
            if len(hits) > 1:
                break
        if len(hits) == 1:
            disp = mem.read(hits[0] + CTX_SIG_DISP, 4)
            if disp:
                target = hits[0] + CTX_SIG_INSN + CTX_SIG_INSN_LEN + int.from_bytes(disp, "little", signed=True)
                rva = target - mem.base
                if 0 < rva < 0x20000000 and mem.pointer(mem.base + rva):
                    return rva
    return CTX_FALLBACK


class Car:

    def __init__(self, mem: _Mem, entity: int, car: int):
        self.mem, self.entity, self.car = mem, entity, car

    def _wheel(self, index, field):
        return self.car + WHEELS_BASE + index * WHEEL_STRIDE + field

    def is_car(self) -> bool:
        rpm = self.mem.f32(self.car + CAR_ENGINE_RPM)
        if rpm is None or not (-50.0 <= rpm * RAD_TO_RPM <= 60000.0):
            return False
        top = self.mem.f32(self.car + CAR_MAX_SPEED)
        return top is None or 0.0 <= top * RAD_TO_RPM <= 60000.0

    def _wheels(self, field):
        out = []
        for i in range(WHEEL_COUNT):
            v = self.mem.f32(self._wheel(i, field))
            if v is None or not math.isfinite(v):
                return None
            out.append(v)
        return out

    @property
    def ride_height(self):
        return self._wheels(RIDE_HEIGHT)

    @property
    def wheel_radius(self):
        return self._wheels(RADIUS)

    def set_ride_height(self, values) -> bool:
        ok = True
        for i, v in enumerate(values[:WHEEL_COUNT]):
            ok = self.mem.write(self._wheel(i, RIDE_HEIGHT), struct.pack("<f", float(v))) and ok
        return ok

    @property
    def camber(self):
        out = []
        for i in range(WHEEL_COUNT):
            d = self.mem.read(self._wheel(i, CAMBER_SIN_LIVE), 8)
            if not d:
                return None
            s, c = struct.unpack("<ff", d)
            if not (math.isfinite(s) and math.isfinite(c)):
                return None
            out.append(2.0 * math.degrees(math.atan2(s, c)))
        return out

    def _camber_table(self, wheel):
        axle = self.mem.pointer(self._wheel(wheel, 0) + TABLE_PTR)
        return self.mem.pointer(axle + TABLE_SUB) if axle else None

    def set_camber(self, values) -> bool:
        ok, written = True, set()
        for wheel, deg in enumerate(values[:WHEEL_COUNT]):
            if deg is None:
                continue
            table = self._camber_table(wheel)
            if not table:
                continue
            key = (table, TABLE_REGION[wheel])
            if key in written:
                continue
            written.add(key)
            half = math.radians(float(deg)) / 2.0
            pair = struct.pack("<ff", math.sin(half), math.cos(half))
            for i in range(TABLE_ENTRY_COUNT):
                entry = table + TABLE_REGION[wheel] + i * TABLE_ENTRY_SIZE
                ok = self.mem.write(entry + TABLE_CAMBER_SIN, pair) and ok
        return bool(written) and ok

    @property
    def media_name(self) -> str | None:
        record = self.mem.pointer(self.entity + ENTITY_TO_RECORD)
        raw = self.mem.read(record, 0x20) if record else None
        if not raw:
            return None
        length, reserved = struct.unpack_from("<QQ", raw, 0x10)
        if length > reserved or length > 255:
            return None
        text = raw[:length] if reserved <= 15 else \
            (self.mem.read(struct.unpack_from("<Q", raw, 0)[0], length) if length else b"")
        try:
            return text.decode("ascii") if text is not None else None
        except UnicodeDecodeError:
            return None


def find_car(mem: _Mem, context: int):
    a = mem.pointer(mem.base + context)
    b = mem.pointer(a + CTX_TO_A) if a else None
    c = mem.pointer(b + A_TO_MGR) if b else None
    entity_list = mem.pointer(c + MGR_TO_LIST) if c else None
    if not entity_list:
        return None, "no car yet"
    begin = mem.pointer(entity_list + LIST_VEC_BEGIN)
    end_raw = mem.read(entity_list + LIST_VEC_END, 8)
    if not begin or not end_raw:
        return None, "no car yet"
    end = struct.unpack("<Q", end_raw)[0]
    if end < begin or (end - begin) % 8 or (end - begin) > 0x100000:
        return None, "no car yet"
    for slot in range(begin, end, 8):
        entity = mem.pointer(slot)
        car = mem.pointer(entity + ENTITY_TO_CAR) if entity else None
        if car:
            candidate = Car(mem, entity, car)
            if candidate.is_car():
                return candidate, None
    return None, "car not readable on this game version"


class AirRide:

    def __init__(self):
        self.settings = dict(DEFAULTS)
        self.lowered = False
        self._lock = threading.Lock()
        self._commands: list[str] = []
        self._pending: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mem: _Mem | None = None
        self._context = None
        self.car: Car | None = None
        self._identity = None
        self.stock = None
        self.radii = None
        self.stock_camber = None
        self._ramp = None
        self._camber_ramp = None
        self._last_scan = 0.0
        self._last_rebake = 0.0
        self.status = "off"
        self._drive = None
        self._drive_held = False
        self._release = False
        self._last_camber_write = (None, 0.0)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hl-airride")
        self._thread.start()

    def stop(self):
        with self._lock:
            self._drive, self._drive_held, self._release = None, False, False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._restore_now()
        self._close()

    def update(self, raw: dict):
        with self._lock:
            self._pending = clean_settings(raw, self._pending or self.settings)

    def command(self, action: str):
        if action in ("up", "down", "toggle"):
            with self._lock:
                if self._drive is not None and not self._drive_held:
                    return
                if self._drive is not None:
                    self._drive, self._drive_held, self._release = None, False, True
                self._commands.append(action)

    def drive(self, settings: dict, heights, cambers=None):
        clean = clean_settings({k: v for k, v in settings.items() if not isinstance(v, list)},
                               DEFAULTS)
        clean["enabled"] = True
        clean["camber"] = bool(settings.get("camber")) and cambers is not None
        h = [max(0.0, min(1.0, float(v))) for v in list(heights)[:WHEEL_COUNT]]
        c = None if cambers is None else [max(0.0, min(1.0, float(v))) for v in list(cambers)[:WHEEL_COUNT]]
        with self._lock:
            self._drive = (clean, h, c)
            self._drive_held = False
            self._release = False

    def release(self, ramp_back: bool = True):
        with self._lock:
            if self._drive is None:
                return
            if ramp_back:
                self._drive, self._drive_held, self._release = None, False, True
            else:
                self._drive_held = True

    @property
    def driven(self) -> bool:
        return self._drive is not None

    def snapshot(self) -> dict:
        s = self.settings
        out = {"enabled": s["enabled"], "lowered": self.lowered, "status": self.status,
               "moving": self._ramp is not None, "car": bool(self.car and self.stock),
               "sequencer": self._drive is not None}
        try:
            if self.stock:
                heights = self._target(self.lowered)
                def pct(w):
                    clear = self._clearance(w)
                    return round(100.0 * (heights[w] - self._radius(w)) / clear) if clear > 0 else 100
                out["front"], out["rear"] = pct(0), pct(3)
        except (TypeError, IndexError):
            pass
        return out

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as error:
                self.status = f"error: {error!r}"[:120]
                self._drop_car()
            self._stop.wait(1.0 / RAMP_HZ)

    def _tick(self):
        now = time.monotonic()
        with self._lock:
            pending, self._pending = self._pending, None
            commands, self._commands = self._commands, []
            drive, release, self._release = self._drive, self._release, False
        if pending is not None:
            self._apply_settings(pending)
        if not (self.settings["enabled"] or drive is not None or release
                or self._ramp is not None or self._camber_ramp is not None):
            self.status = "off"
            return
        if not self._attach():
            return
        if now - self._last_scan >= RESCAN_SECONDS:
            self._last_scan = now
            self._rescan(settle=drive is None)
            if not self.car:
                return
        if drive is not None:
            self._apply_drive(drive, now)
            return
        if release:
            self._hand_back()
        if not self.settings["enabled"] and self._ramp is None and self._camber_ramp is None:
            self.status = "off"
            return
        for action in commands:
            self._do(action)
        self._animate(now)
        if self._ramp is None and self._camber_ramp is None and now - self._last_rebake >= REBAKE_SECONDS:
            self._last_rebake = now
            self._reapply_if_rebaked()

    def _attach(self) -> bool:
        if self._mem and self._mem.alive:
            return True
        self._close()
        pid = game.game_pid()
        if not pid:
            self.status = "game not running"
            return False
        from .camera_scan import _main_module_base
        base, _ = _main_module_base(pid)
        if not base:
            self.status = "game not readable"
            return False
        try:
            self._mem = _Mem(pid, base)
        except OSError as error:
            self.status = str(error)
            return False
        self._identity = None
        self.stock = self.radii = self.stock_camber = None
        self.lowered = False
        self._context = resolve_context(self._mem)
        return True

    def _close(self):
        self._drop_car()
        if self._mem:
            self._mem.close()
        self._mem = None

    def _drop_car(self):
        self.car = None
        self._ramp = self._camber_ramp = None

    def _rescan(self, settle=True):
        car, problem = find_car(self._mem, self._context)
        if car is None:
            self.status = problem
            self._drop_car()
            return
        name = car.media_name
        identity = (name, car.entity, car.car)
        old = self._identity
        if old is not None and (name is None or old[0] is None) and identity[1:] == old[1:]:
            identity = (name or old[0], car.entity, car.car)
            new_car = False
        else:
            new_car = old is None or name != old[0]
        if new_car:
            self._identity = identity
            self.car = car
            self.lowered = False
            self._ramp = self._camber_ramp = None
            self.stock = self.radii = self.stock_camber = None
            self._capture_stock()
            if settle and self.settings["enabled"]:
                self._settle_to(self._target(False))
        elif identity != self._identity:
            self._identity = identity
            self.car = car
            self._ramp = self._camber_ramp = None
            if self.stock and settle and self.settings["enabled"]:
                car.set_ride_height(self._target(self.lowered))
                self._write_active_camber()
        else:
            self.car = car
            if self.stock is None:
                self._capture_stock()
        self.status = "ready" if self.stock else "reading car"

    def _capture_stock(self):
        car = self.car
        heights = car.ride_height
        if not heights or not all(0.01 < h < 2.0 for h in heights):
            return
        radii = car.wheel_radius
        self.radii = list(radii) if radii and all(0.05 < r < 1.0 for r in radii) else None
        self.stock = list(heights)
        self.stock_camber = car.camber

    def _radius(self, wheel):
        return self.radii[wheel] if self.radii else 0.0

    def _clearance(self, wheel):
        return max(0.0, self.stock[wheel] - self._radius(wheel))

    def _clamp(self, value, wheel, settings=None):
        s = settings or self.settings
        floor = max(self._clearance(wheel) * (min(max(s["floor"], 0.0), 100.0) / 100.0),
                    HARD_FLOOR_M)
        return max(floor + self._radius(wheel), value)

    def _baseline(self, settings=None):
        s = settings or self.settings
        percents = (s["front"], s["front"], s["rear"], s["rear"])
        return [self._clamp(self.stock[w] + self._clearance(w) * percents[w] / 100.0, w, s)
                for w in range(WHEEL_COUNT)]

    def _target(self, lowered, settings=None):
        s = settings or self.settings
        base = self._baseline(s)
        if not lowered:
            return base
        return [self._clamp(v - self._clearance(w) * s["drop"] / 100.0, w, s)
                for w, v in enumerate(base)]

    def _air_camber_target(self, settings=None):
        s = settings or self.settings
        f, r = s["camber_front"], s["camber_rear"]
        return [f, -f, -r, r]

    def _active_camber_target(self):
        if not self.settings["camber"] or not self.stock_camber:
            return None
        return self._air_camber_target() if self.lowered else list(self.stock_camber)

    def _write_active_camber(self):
        target = self._active_camber_target()
        if target and self.car:
            self.car.set_camber(target)

    def _do(self, action):
        if not self.stock or not self.car:
            return
        lower = (not self.lowered) if action == "toggle" else (action == "down")
        if lower == self.lowered:
            return
        start_camber = self.car.camber if self.settings["camber"] else None
        self.lowered = lower
        start = self.car.ride_height or self._target(not lower)
        self._start_ramp(start, self._target(lower), self.settings["speed"], reverse=not lower)
        target = self._active_camber_target()
        if target and start_camber:
            self._camber_ramp = (list(start_camber), target, time.monotonic(), self.settings["speed"])

    def drive_targets(self, settings, heights, cambers):
        raised, lowered = self._target(False, settings), self._target(True, settings)
        h = [a + (b - a) * f for a, b, f in zip(raised, lowered, heights)]
        cam = None
        if cambers is not None and settings.get("camber") and self.stock_camber:
            air = self._air_camber_target(settings)
            cam = [a + (b - a) * f for a, b, f in zip(self.stock_camber, air, cambers)]
        return h, cam

    def _apply_drive(self, drive, now):
        settings, heights, cambers = drive
        if not self.stock or not self.car:
            self.status = "reading car"
            return
        self._ramp = self._camber_ramp = None
        h, cam = self.drive_targets(settings, heights, cambers)
        if not self.car.set_ride_height(h):
            self._drop_car()
            return
        last, at = self._last_camber_write
        if cam is not None and (last is None or any(abs(a - b) > 0.01 for a, b in zip(cam, last))
                                or now - at > 0.5):
            self.car.set_camber(cam)
            self._last_camber_write = (cam, now)
            self._sequencer_camber = True
        elif cam is None and getattr(self, "_sequencer_camber", False) and self.stock_camber:
            self.car.set_camber(self.stock_camber)
            self._sequencer_camber = False
            self._last_camber_write = (None, 0.0)
        self.status = "sequencer"

    def _hand_back(self):
        if not self.car or not self.stock:
            return
        start = self.car.ride_height or self._target(False)
        end = self._target(self.lowered) if self.settings["enabled"] else list(self.stock)
        self._start_ramp(start, end, SETTLE_SECONDS)
        if getattr(self, "_sequencer_camber", False):
            self._sequencer_camber = False
            self._last_camber_write = (None, 0.0)
            target = self._active_camber_target() if self.settings["enabled"] else None
            back = target or self.stock_camber
            current = self.car.camber
            if back and current:
                self._camber_ramp = (list(current), list(back), time.monotonic(), SETTLE_SECONDS)
        if not self.settings["enabled"]:
            self.lowered = False

    def _start_ramp(self, start, end, seconds, reverse=False):
        self._ramp = (list(start), list(end), time.monotonic(), max(0.05, seconds), reverse)

    def _settle_to(self, end):
        if self.car and self.stock and end:
            start = self.car.ride_height or end
            self._start_ramp(start, end, SETTLE_SECONDS)

    def _axle_phase(self, wheel, reverse):
        seq = self.settings["sequence"]
        if seq == "together":
            return 0.0, 1.0
        front_leads = (seq == "front") != reverse
        leads = (wheel in FRONT_WHEELS) == front_leads
        return (0.0 if leads else 1.0 - AXLE_OVERLAP), AXLE_OVERLAP

    def _animate(self, now):
        if self._ramp:
            start, end, t0, seconds, reverse = self._ramp
            progress = min(1.0, (now - t0) / seconds)
            values = []
            for w in range(WHEEL_COUNT):
                lead, span = self._axle_phase(w, reverse)
                f = min(1.0, max(0.0, (progress - lead) / span)) if span > 0 else 1.0
                eased = f * f * (3.0 - 2.0 * f)
                values.append(start[w] + (end[w] - start[w]) * eased)
            if not self.car.set_ride_height(values):
                self._drop_car()
                return
            if progress >= 1.0:
                self._ramp = None
        if self._camber_ramp and self.car:
            start, end, t0, seconds = self._camber_ramp
            progress = min(1.0, (now - t0) / max(0.05, seconds))
            frac = curve_value(CAMBER_CURVES[self.settings["curve"]], progress)
            self.car.set_camber([a + (b - a) * frac for a, b in zip(start, end)])
            if progress >= 1.0:
                self._camber_ramp = None

    def _reapply_if_rebaked(self):
        if not self.stock or not self.car or not self.settings["enabled"]:
            return
        expected = self._target(self.lowered)
        current = self.car.ride_height
        if current and any(abs(a - b) > 1e-3 for a, b in zip(current, expected)):
            self.car.set_ride_height(expected)
        target = self._active_camber_target()
        if target:
            current = self.car.camber
            if current and any(abs(a - b) > 0.05 for a, b in zip(current, target)):
                self.car.set_camber(target)

    def _apply_settings(self, new):
        old, self.settings = self.settings, new
        if not old["enabled"] and new["enabled"]:
            self.lowered = False
            self._last_scan = 0.0
            return
        if old["enabled"] and not new["enabled"]:
            if self._drive is None:
                self._restore_now(ramp=True)
            return
        if not new["enabled"] or not self.car or not self.stock:
            return
        height_keys = ("front", "rear", "drop", "floor")
        if any(old[k] != new[k] for k in height_keys):
            self._settle_to(self._target(self.lowered))
        if old["camber"] and not new["camber"]:
            self._camber_ramp = None
            if self.stock_camber:
                self.car.set_camber(self.stock_camber)
        elif new["camber"] and (not old["camber"] or self.lowered and (
                old["camber_front"] != new["camber_front"] or old["camber_rear"] != new["camber_rear"])):
            self._camber_ramp = None
            self._write_active_camber()

    def _restore_now(self, ramp=False):
        car, stock, stock_camber = self.car, self.stock, self.stock_camber
        self._ramp = self._camber_ramp = None
        if car and stock:
            seq_camber = getattr(self, "_sequencer_camber", False)
            self._sequencer_camber = False
            try:
                if ramp:
                    start = car.ride_height or stock
                    t0, seconds = time.monotonic(), SETTLE_SECONDS
                    while True:
                        f = min(1.0, (time.monotonic() - t0) / seconds)
                        e = f * f * (3.0 - 2.0 * f)
                        car.set_ride_height([a + (b - a) * e for a, b in zip(start, stock)])
                        if f >= 1.0:
                            break
                        time.sleep(1.0 / RAMP_HZ)
                else:
                    car.set_ride_height(stock)
                if stock_camber and (self.settings["camber"] or seq_camber):
                    car.set_camber(stock_camber)
            except Exception:
                pass
        self.lowered = False
        self.stock = self.radii = self.stock_camber = None
        self._identity = None
        self.car = None
