from __future__ import annotations

import json
import math
import os
from pathlib import Path

from . import rcf6


CONTROL_RECENTER = 2
CONTROL_MODE = 3
CONTROL_FREECAM_MOVE = 4
CONTROL_SELECT_PRESET = 5
CONTROL_YAW = 6
CONTROL_FRAME_NUDGE = 7
CONTROL_SAVE_PRESET = 8
CONTROL_FREECAM_OPTIONS = 15
CONTROL_RUN = 23
CONTROL_STEADYCAM = 24
CONTROL_MOTION_FX = 90
CONTROL_WALKING_FX = 91

FRAME_PASSENGER = 1
FRAME_WINDOW = 2
FRAME_BUMPER = 3
FRAME_WHEEL = 4
FRAME_DRIVER = 5
FRAME_GAP = 6
FRAME_NAMES = {FRAME_PASSENGER: "passenger", FRAME_WINDOW: "window",
               FRAME_BUMPER: "bumper", FRAME_WHEEL: "wheel",
               FRAME_DRIVER: "driver", FRAME_GAP: "gap"}
CENTRE_LINE_FRAMES = (FRAME_GAP,)
SIDE_MIRRORED_FRAMES = (FRAME_WINDOW, FRAME_WHEEL)
FACTORY_FRAME_OFFSETS = {
    FRAME_PASSENGER: (0.0, 0.0, 0.0),
    FRAME_WINDOW: (-0.19717124089391488, 0.053720164464260395,
                   -0.28006977219562706),
    FRAME_BUMPER: (0.0008466358851509475, -0.49504638939364987,
                   0.039446032907806995),
    FRAME_WHEEL: (-0.10325214209520513, -0.5044922421128953,
                  -0.9022473459824091),
    FRAME_DRIVER: (0.007073600242995682, -0.03840102377805256,
                   0.10012497764890749),
    FRAME_GAP: (0.0, 0.0, 0.0),
}
FALLBACK_DIMS = (1.8, 1.4, 4.5)

PASSENGER_X_FALLBACK = 0.0
PASSENGER_Y = -0.05
PASSENGER_Z = 0.0

FORZA_FREECAM_SPEED_SCALE = 0.32
FORZA_RUN_SPEED_MULTIPLIER = 2.5
FORZA_PHONE_SPEED_MAXIMUM = 5.0
FORZA_FAST_SPEED_MAXIMUM = 12.0
FORZA_DRONE_SPEED_MAXIMUM = 30.0
RAW_FREECAM_MOVEMENT_LIMIT = 20.0
HELD_MOVEMENT_SECONDS = 0.35


def desired_camera(car: bool, frame_preset: int) -> str:
    if not car:
        return "follow_low"
    if frame_preset in (FRAME_PASSENGER, FRAME_DRIVER):
        return "driver"
    return "follow_low"


def q_norm(q):
    n = math.sqrt(sum(v * v for v in q))
    if n < 1e-08:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(v / n for v in q)


def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def q_slerp(prev, target, alpha):
    prev = q_norm(prev)
    target = q_norm(target)
    dot = sum(a * b for a, b in zip(prev, target))
    if dot < 0.0:
        target = tuple(-v for v in target)
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        return q_norm(tuple(a + alpha * (b - a) for a, b in zip(prev, target)))
    angle = math.acos(dot)
    sine = math.sin(angle)
    first = math.sin((1.0 - alpha) * angle) / sine
    second = math.sin(alpha * angle) / sine
    return tuple(first * a + second * b for a, b in zip(prev, target))


def level_heading(q):
    x, y, z, w = q_norm(q)
    fx = 2.0 * (x * z + w * y)
    fz = 1.0 - 2.0 * (x * x + y * y)
    n = math.hypot(fx, fz)
    return (fx / n, fz / n) if n > 0.2 else None


def q_from_euler_yxz_deg(pitch, yaw, roll):
    return rcf6.euler_to_quaternion(pitch, yaw, roll)


def phone_rotation(euler, turn_yaw=0.0):
    pitch, yaw, roll = euler
    yaw = math.remainder(yaw + turn_yaw, 360.0)
    return q_from_euler_yxz_deg(-pitch, -yaw, roll)


class PhoneControls:

    def __init__(self):
        self.car = True
        self.reset = 0
        self.yaw = 0.0
        self.movement = (0.0, 0.0, 0.0)
        self.movement_at = -math.inf

    def _reset_origin(self):
        self.reset = (self.reset + 1) & 0xFFFFFFFF
        self.yaw = 0.0
        self.movement = (0.0, 0.0, 0.0)

    def set_freecam(self, freecam: bool):
        car = not freecam
        if car != self.car:
            self.car = car
            self._reset_origin()

    def control(self, control_id, value, movement, now):
        if control_id == CONTROL_RECENTER:
            self._reset_origin()
        elif control_id == CONTROL_MODE:
            self.set_freecam(value > 0.5)
        elif control_id in (CONTROL_FREECAM_MOVE, CONTROL_FRAME_NUDGE):
            if not self.car:
                limit = RAW_FREECAM_MOVEMENT_LIMIT
                self.movement = tuple(max(-limit, min(limit, v))
                                      for v in movement)
            else:
                self.movement = tuple(max(-1.0, min(1.0, v)) * 0.5
                                      for v in movement)
            self.movement_at = now
        elif control_id == CONTROL_YAW:
            self.yaw = math.remainder(self.yaw + value, 360.0)

    def held_movement(self, now, last_fresh_at):
        if now - last_fresh_at <= HELD_MOVEMENT_SECONDS:
            return self.movement
        return (0.0, 0.0, 0.0)


def frame_presets_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "HorizonLens" / "forza-frame-presets.v1.json"


class SessionFraming:

    STORE_POLL_SECONDS = 1.0

    def __init__(self, speed=0.5, path: Path | None = None, event_sink=None):
        self.speed = speed
        self.path = Path(path) if path is not None else None
        self.event_sink = event_sink or (lambda _message: None)
        self.selected = FRAME_PASSENGER
        self.last_car = None
        self.passenger_side = 1.0
        self.offsets = {preset: list(FACTORY_FRAME_OFFSETS[preset])
                        for preset in FACTORY_FRAME_OFFSETS}
        self.saved: dict[int, tuple] = {}
        self.stick = (0.0, 0.0, 0.0)
        self.velocity = (0.0, 0.0, 0.0)
        self.updated_at = None
        self.next_store_poll_at = 0.0
        self.store_signature = None
        self._reload_store(apply=True)

    @property
    def offset(self):
        return self.offsets[self.selected]

    @staticmethod
    def _preset(value, fallback):
        try:
            preset = int(round(value))
        except (TypeError, ValueError, OverflowError):
            return fallback
        return preset if preset in FACTORY_FRAME_OFFSETS else fallback

    def _event(self, message):
        try:
            self.event_sink(f"FORZA_FRAMING {message}")
        except (OSError, ValueError):
            pass

    def _load_selected(self):
        self.offset[:] = self.saved.get(
            self.selected, FACTORY_FRAME_OFFSETS[self.selected])

    @staticmethod
    def _validated_offsets(document):
        if not isinstance(document, dict) or document.get("version") != 1 \
                or document.get("gameId") != "forza-horizon-6":
            return None
        offsets = document.get("offsets")
        if not isinstance(offsets, dict):
            return None
        result = {}
        for preset in FACTORY_FRAME_OFFSETS:
            value = offsets.get(str(preset))
            if value is None:
                continue
            if not (isinstance(value, list) and len(value) == 3 and all(
                    isinstance(v, (int, float)) and math.isfinite(v)
                    and abs(v) <= 10 for v in value)):
                return None
            result[preset] = tuple(float(v) for v in value)
        return result

    def _signature(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload_store(self, apply=False):
        if self.path is None:
            return
        signature = self._signature()
        self.store_signature = signature
        if signature is None:
            self.saved = {}
        else:
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            saved = self._validated_offsets(document)
            if saved is None:
                return
            self.saved = saved
        if apply:
            self._load_selected()

    def _persist(self):
        if self.path is None:
            return
        document = {
            "version": 1,
            "gameId": "forza-horizon-6",
            "offsets": {str(preset): list(offset)
                        for preset, offset in sorted(self.saved.items())},
        }
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, indent=2) + "\n",
                                 encoding="utf-8")
            os.replace(temporary, self.path)
            self.store_signature = self._signature()
            self._event(f"saved preset={self.selected} "
                        f"offset={tuple(self.offset)}")
        except OSError as error:
            try:
                temporary.unlink()
            except OSError:
                pass
            self._event(f"save_failed preset={self.selected} "
                        f"error={type(error).__name__}")

    def _poll_store(self, now):
        if self.path is None or now < self.next_store_poll_at:
            return
        self.next_store_poll_at = now + self.STORE_POLL_SECONDS
        if self._signature() != self.store_signature:
            self._reload_store(apply=False)

    def control(self, control_id, value, movement, car):
        if control_id == CONTROL_FRAME_NUDGE and car:
            x, y, z = (max(-1.0, min(1.0, v)) for v in movement)
            self.stick = (-x, y, z)
        else:
            self.stick = (0.0, 0.0, 0.0)
        if control_id == CONTROL_SAVE_PRESET:
            preset = self._preset(value, self.selected)
            self.selected = preset
            self.saved[preset] = tuple(self.offset)
            self._persist()
        elif control_id == CONTROL_SELECT_PRESET:
            self.selected = self._preset(value, self.selected)
            self._load_selected()
        elif control_id == CONTROL_RECENTER:
            self._load_selected()

    def update(self, now, car, heading=None):
        self._poll_store(now)
        if car and self.last_car is False:
            self._load_selected()
            self.stick = (0.0, 0.0, 0.0)
            self.velocity = (0.0, 0.0, 0.0)
            self._event(f"free_to_car frame_preset={self.selected}")
        self.last_car = car
        if self.updated_at is None:
            self.updated_at = now
        elapsed = max(0.0, min(0.05, now - self.updated_at))
        self.updated_at = now
        right, up, forward = self.stick
        fx, fz = heading if heading else (0.0, 1.0)
        target = [(right * fz + forward * fx) * self.speed, up * self.speed,
                  (-right * fx + forward * fz) * self.speed]
        if self.selected in CENTRE_LINE_FRAMES:
            target[0] = 0.0
        alpha = 1.0 - math.exp(-elapsed / 0.04) if elapsed > 0 else 0.0
        self.velocity = tuple(v + (t - v) * alpha
                              for v, t in zip(self.velocity, target))
        if car:
            lateral = (self.passenger_side
                       if self.selected in SIDE_MIRRORED_FRAMES else 1.0)
            for axis in range(3):
                self.offset[axis] += (self.velocity[axis] * elapsed
                                      * (lateral if axis == 0 else 1.0))
        return tuple(self.offset)

    def pause(self):
        self.stick = (0.0, 0.0, 0.0)
        self.velocity = (0.0, 0.0, 0.0)
        self.updated_at = None

    def car_anchor(self, passenger_x, passenger_y, passenger_z,
                   vehicle_dimensions):
        passenger_side = -1.0 if passenger_x < 0 else 1.0
        self.passenger_side = passenger_side
        driver_to_centre = passenger_x * 0.5
        try:
            width, height, length = vehicle_dimensions
            if not (0.5 <= width <= 5 and 0.3 <= height <= 4
                    and 1 <= length <= 15):
                raise ValueError
        except (TypeError, ValueError):
            width, height, length = FALLBACK_DIMS
        if self.selected == FRAME_PASSENGER:
            base = (passenger_x, passenger_y, passenger_z)
        elif self.selected == FRAME_DRIVER:
            base = (0.0, 0.0, 0.0)
        elif self.selected == FRAME_WINDOW:
            base = (passenger_x + passenger_side * max(0.48, width * 0.38),
                    passenger_y + height * 0.13,
                    passenger_z + length * 0.04)
        elif self.selected == FRAME_BUMPER:
            base = (driver_to_centre,
                    passenger_y + height * 0.08,
                    passenger_z + length * 0.58)
        elif self.selected == FRAME_GAP:
            base = (driver_to_centre,
                    passenger_y - height * 0.15,
                    passenger_z - length * 0.42 - 0.35)
        else:
            base = (driver_to_centre
                    + passenger_side * (width * 0.5 + 0.18),
                    passenger_y + height * 0.02,
                    passenger_z + length * 0.27)
        offset = list(self.offset)
        if self.selected in SIDE_MIRRORED_FRAMES:
            offset[0] *= passenger_side
        return tuple(value + adjustment
                     for value, adjustment in zip(base, offset))


class FreecamOptions:

    def __init__(self):
        self.ignore_ground_height = False
        self.lock_movement_direction = False
        self.automatic_stand_up = True
        self.drone_inertia = False

    def control(self, control_id, value):
        if control_id != CONTROL_FREECAM_OPTIONS:
            return
        options = max(0, min(15, int(round(value))))
        self.ignore_ground_height = bool(options & 1)
        self.lock_movement_direction = bool(options & 2)
        self.automatic_stand_up = bool(options & 4)
        self.drone_inertia = bool(options & 8)

    def spawn_offset(self, passenger_x):
        if abs(passenger_x) < 0.05:
            lateral = 0.0
        else:
            lateral = passenger_x + math.copysign(0.45, passenger_x)
        return (lateral, 0.5 if self.automatic_stand_up else 0.0, 0.0)

    def wire_options(self, drone_inertia_supported,
                     synchronized_pose_filter_supported,
                     steadycam_supported, steadycam):
        options = 1 if self.ignore_ground_height else 0
        if self.drone_inertia and drone_inertia_supported:
            options |= 2
        if synchronized_pose_filter_supported:
            options |= 4
        if steadycam and steadycam_supported:
            options |= 8
        return options


class Toggle:

    def __init__(self, control_id):
        self.control_id = control_id
        self.enabled = False

    def control(self, control_id, value):
        if control_id == self.control_id:
            self.enabled = value >= 0.5


def fx_activity(car, drone_inertia, steadycam_enabled):
    effects = not drone_inertia and not steadycam_enabled
    return (effects and not car, effects)


def _forza_fov_native(phone_zoom, previous=70.0):
    try:
        z = float(phone_zoom)
    except (TypeError, ValueError):
        return previous
    if not (math.isfinite(z) and 0.0 <= z <= 1.0):
        return previous
    if z <= 0.2:
        t = z / 0.2
        t2, t3 = t * t, t * t * t
        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + t
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2
        return (120.0 * h00 + (0.2 * -300.0) * h10
                + 70.0 * h01 + (0.2 * -68.75) * h11)
    return 70.0 - 68.75 * (z - 0.2)


NATIVE_FOV = 70.0


def forza_fov(phone_zoom, previous=70.0, base=NATIVE_FOV):
    try:
        z = float(phone_zoom)
    except (TypeError, ValueError):
        return previous
    if not (math.isfinite(z) and 0.0 <= z <= 1.0):
        return previous
    fov = _forza_fov_native(z, previous)
    if base == NATIVE_FOV:
        return fov
    k = math.tan(math.radians(base) / 2) / math.tan(math.radians(NATIVE_FOV) / 2)
    widened = math.degrees(2 * math.atan(math.tan(math.radians(fov) / 2) * k))
    return max(10.0, min(120.0, widened))


MPS_TO_MPH = 2.2369362920544
GAP_ZOOM_POINTS = ((100.0, 1.0), (200.0, 0.5), (250.0, 0.3))
GAP_WIDEST_FOV_FACTOR = 0.5
GAP_MAX_PULL = 6.0


def phone_zoom_to_factor(z):
    z = max(0.0, min(1.0, float(z)))
    return 0.5 + z / 0.2 * 0.5 if z <= 0.2 else 1.0 + (z - 0.2) / 0.8 * 2.0


def factor_to_phone_zoom(f):
    f = max(0.5, min(3.0, float(f)))
    return (f - 0.5) / 0.5 * 0.2 if f <= 1.0 else 0.2 + (f - 1.0) / 2.0 * 0.8


GAP_START_MPH = 100.0
GAP_TOP_MPH = 250.0
GAP_MAX_OUT = 0.3


def gap_speed_curve(mph, start=GAP_START_MPH, max_out=GAP_MAX_OUT):
    start = max(20.0, min(200.0, float(start)))
    max_out = max(0.1, min(0.95, float(max_out)))
    if mph <= start:
        return 1.0
    u = GAP_ZOOM_POINTS[0][0] + (mph - start) * (GAP_TOP_MPH - GAP_ZOOM_POINTS[0][0]) / (GAP_TOP_MPH - start)
    depth = (1.0 - gap_speed_factor(u)) / (1.0 - GAP_ZOOM_POINTS[-1][1])
    return 1.0 - depth * (1.0 - max_out)


def gap_speed_factor(mph, points=GAP_ZOOM_POINTS):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if mph <= xs[0]:
        return ys[0]
    if mph >= xs[-1]:
        return ys[-1]
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [0.0] * n
    for i in range(1, n - 1):
        if d[i - 1] * d[i] > 0:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    i = max(k for k in range(n - 1) if xs[k] <= mph)
    t = (mph - xs[i]) / h[i]
    t2, t3 = t * t, t * t * t
    return ((2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
            + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1])


class GapZoom:

    TIME_CONSTANT = 0.6

    def __init__(self):
        self.reset()

    def reset(self):
        self.value = None
        self.updated_at = None

    def update(self, target, now):
        target = max(0.05, float(target))
        if self.value is None or self.updated_at is None:
            self.value, self.updated_at = target, now
            return self.value
        elapsed = max(0.0, min(0.25, now - self.updated_at))
        self.updated_at = now
        alpha = 1.0 - math.exp(-elapsed / self.TIME_CONSTANT)
        self.value = math.exp(math.log(self.value) + (math.log(target) - math.log(self.value)) * alpha)
        return self.value


def gap_lens(factor, fov_base, car_length):
    if factor >= GAP_WIDEST_FOV_FACTOR:
        return forza_fov(factor_to_phone_zoom(factor), base=fov_base), 0.0
    subject = 0.55 * car_length + 0.35
    pull = subject * (GAP_WIDEST_FOV_FACTOR / max(0.05, factor) - 1.0)
    return forza_fov(0.0, base=fov_base), min(GAP_MAX_PULL, pull)


def pull_back(output, metres):
    if metres <= 0.0:
        return output
    qx, qy, qz, qw = output["quaternion"]
    fwd = (2.0 * (qx * qz + qw * qy), 2.0 * (qy * qz - qw * qx), 1.0 - 2.0 * (qx * qx + qy * qy))
    return dict(output, xyz=tuple(p - f * metres for p, f in zip(output["xyz"], fwd)))


class PoseSmoother:

    def __init__(self, time_constant=0.035):
        if not (math.isfinite(time_constant) and 0.0 < time_constant <= 0.5):
            raise ValueError("Pose smoothing time constant must be within "
                             "0.5 seconds.")
        self.time_constant = time_constant
        self.value = None
        self.updated_at = None

    def reset(self):
        self.value = None
        self.updated_at = None

    def update(self, value, now, smooth_pose=True):
        elapsed = 0.0 if self.updated_at is None else max(
            0.0, min(0.1, now - self.updated_at))
        if (self.value is None or self.updated_at is None
                or self.value["mode"] != value["mode"]
                or self.value["recenter"] != value["recenter"]):
            self.value = dict(value)
            self.updated_at = now
            return self.value
        alpha = 1.0 - math.exp(-elapsed / self.time_constant)
        position = tuple(a + (b - a) * alpha
                         for a, b in zip(self.value["xyz"], value["xyz"])) \
            if smooth_pose else value["xyz"]
        rotation = q_slerp(self.value["quaternion"], value["quaternion"],
                           alpha) if smooth_pose else value["quaternion"]
        self.value = dict(value, xyz=position, quaternion=rotation)
        self.updated_at = now
        return self.value


def scale_forza_freecam_movement(movement, run_enabled=False, drone=False):
    multiplier = FORZA_RUN_SPEED_MULTIPLIER if run_enabled else 1.0
    maximum = FORZA_DRONE_SPEED_MAXIMUM if drone else FORZA_FAST_SPEED_MAXIMUM
    gain = (FORZA_FREECAM_SPEED_SCALE * multiplier * maximum
            / FORZA_PHONE_SPEED_MAXIMUM)
    scaled = tuple(c * gain for c in movement)
    magnitude = math.sqrt(sum(c * c for c in scaled))
    if magnitude <= maximum:
        return scaled
    return tuple(c * maximum / magnitude for c in scaled)


class JoystickMotionSmoother:

    def __init__(self, maximum=FORZA_DRONE_SPEED_MAXIMUM + 0.5,
                 time_constant=0.04):
        self.maximum = maximum
        self.time_constant = time_constant
        self.reset()

    def reset(self):
        self.value = (0.0, 0.0, 0.0)
        self.updated_at = None

    def update(self, movement, now, temporal_smoothing=True):
        target = tuple(max(-self.maximum, min(self.maximum, c))
                       for c in movement)
        if not temporal_smoothing or self.updated_at is None:
            self.updated_at = now
            self.value = target
            return self.value
        elapsed = max(0.0, min(0.1, now - self.updated_at))
        self.updated_at = now
        alpha = 1.0 - math.exp(-elapsed / self.time_constant)
        self.value = tuple(p + (c - p) * alpha
                           for p, c in zip(self.value, target))
        return self.value


class WalkingFx:

    INTENSITY_SCALE = 1.0

    def __init__(self):
        self.settings = (False, 1.0, 1.0, 1.0)
        self.reset()

    def reset(self, now=None):
        self.phase = 0.0
        self.blend = 0.0
        self.pace = 0.0
        self.direction_x = 0.0
        self.direction_z = 0.0
        self.updated_at = now

    @staticmethod
    def _alpha(rate, elapsed):
        return 1.0 - math.exp(-rate * elapsed)

    @staticmethod
    def steps_per_second(speed_mps):
        return max(1.5, min(3.0, 1.45 + 0.4 * speed_mps))

    def update(self, movement, now, active=True):
        zero = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        enabled, setting_intensity, speed, global_gain = self.settings
        if not (active and enabled):
            self.reset(now)
            return zero
        if self.updated_at is None:
            self.updated_at = now
        raw_elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        if raw_elapsed >= 0.25:
            self.reset(now)
            raw_elapsed = 0.0
        elapsed = min(0.05, raw_elapsed)
        mx, mz = movement[0], movement[2]
        velocity = math.hypot(mx, mz)
        moving = velocity > 0.15
        self.pace += (velocity - self.pace) * self._alpha(3.0 if velocity > self.pace else 2.2, elapsed)
        target = min(1.0, self.pace / 0.8) if moving or self.pace > 0.15 else 0.0
        self.blend += (target - self.blend) * self._alpha(3.5 if target > self.blend else 2.5, elapsed)
        walking_x = max(-1.0, min(1.0, mx / 3.0))
        walking_z = max(-1.0, min(1.0, mz / 3.0))
        direction_alpha = self._alpha(3.0, elapsed)
        self.direction_x += (walking_x - self.direction_x) * direction_alpha
        self.direction_z += (walking_z - self.direction_z) * direction_alpha
        lead_x = walking_x - self.direction_x
        lead_z = walking_z - self.direction_z
        if self.blend <= 0.0005:
            return zero
        pace = max(self.pace, 0.6)
        stride_hz = self.steps_per_second(pace) / 2.0 * max(0.25, min(3.0, speed))
        self.phase = (self.phase + elapsed * math.tau * stride_hz) % (2.0 * math.tau)
        k = setting_intensity * global_gain * self.blend * self.INTENSITY_SCALE
        run = max(0.0, min(1.0, (pace - 2.5) / 3.0))
        bob_amp = 0.007 + 0.012 * min(1.0, pace / 4.0)
        sway_amp = 0.011 * (1.0 - 0.6 * run)
        step = math.cos(2.0 * self.phase)
        bob = (step + 0.15 * math.cos(4.0 * self.phase)) / 1.15
        sway = math.sin(self.phase)
        translation = (
            (sway * sway_amp + lead_x * 0.006) * k,
            bob * bob_amp * k,
            (-lead_z * 0.006) * k,
        )
        rotation = (
            (-0.35 * math.cos(2.0 * self.phase + 0.5) * (0.6 + run) + lead_z * 1.2) * k,
            (0.25 * sway) * k,
            (-0.45 * sway * (1.0 - 0.4 * run) - lead_x * 1.0) * k,
        )
        return (
            tuple(max(-0.06, min(0.06, v)) for v in translation),
            tuple(max(-4.0, min(4.0, v)) for v in rotation),
        )


class VehicleMotionFx:
    GRAVITY = 9.80665
    SETTINGS = {
        "enabled": False,
        "global_gain": 1.0,
        "g_force": (True, 1.0, 1.0),
        "impacts": (True, 1.0, 1.0),
        "road": (True, 1.0, 1.0),
        "breathing": (True, 1.0, 1.0),
        "tremor": (True, 1.0, 1.0),
        "sway": (True, 1.0, 1.0),
    }

    def __init__(self):
        self.settings = dict(self.SETTINGS)
        self.reset()

    def reset(self, now=None):
        self.conditioned_acceleration = (0.0, 0.0, 0.0)
        self.vertical_slow = 0.0
        self.impact = 0.0
        self.phase = 0.0
        self.telemetry_sequence = 0
        self.telemetry_at = 0.0
        self.updated_at = now

    @staticmethod
    def _alpha(rate, elapsed):
        return 1.0 - math.exp(-rate * elapsed)

    @staticmethod
    def _add(left, right, scale=1.0):
        return tuple(value + offset * scale
                     for value, offset in zip(left, right))

    def _effect(self, name):
        value = self.settings.get(name, (False, 0.0, 1.0))
        if not isinstance(value, tuple) or len(value) != 3:
            return (False, 0.0, 1.0)
        return value

    def update(self, telemetry, now, free_camera=False, active=True):
        zero = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        if not active or self.settings.get("enabled") is not True:
            self.reset(now)
            return zero
        if self.updated_at is None:
            self.updated_at = now
        raw_elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        if raw_elapsed >= 0.25:
            self.reset(now)
            raw_elapsed = 0.0
        elapsed = min(0.05, raw_elapsed)
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        telemetry_fresh = bool(
            telemetry.get("active") is True
            and (now - float(telemetry.get("received_at", 0.0) or 0.0)) <= 0.5)
        telemetry_sequence = telemetry.get("sequence", 0)
        if (telemetry_fresh and isinstance(telemetry_sequence, int)
                and telemetry_sequence != self.telemetry_sequence):
            acceleration = telemetry.get("acceleration", (0.0, 0.0, 0.0))
            if not (isinstance(acceleration, tuple)
                    and len(acceleration) == 3
                    and all(isinstance(v, (int, float))
                            and math.isfinite(v) for v in acceleration)):
                acceleration = (0.0, 0.0, 0.0)
            acceleration = tuple(
                max(-50.0, min(50.0, float(v))) for v in acceleration)
            received_at = float(telemetry.get("received_at", now))
            telemetry_elapsed = (received_at - self.telemetry_at
                                 if self.telemetry_at else 0.0)
            previous = self.conditioned_acceleration
            if 0.0 < telemetry_elapsed <= 0.1:
                blend = self._alpha(9.0, telemetry_elapsed)
                self.conditioned_acceleration = tuple(
                    old + (new - old) * blend
                    for old, new in zip(previous, acceleration))
                delta_g = (math.sqrt(sum(
                    (value - old) ** 2
                    for value, old in zip(self.conditioned_acceleration,
                                          previous))) / self.GRAVITY)
                self.impact = max(self.impact, max(0.0, min(1.0,
                    (delta_g - 0.08) * 0.9)))
            else:
                self.conditioned_acceleration = acceleration
                self.vertical_slow = acceleration[1] / self.GRAVITY
            self.telemetry_sequence = telemetry_sequence
            self.telemetry_at = received_at
        vehicle_effects = telemetry_fresh and not free_camera
        speed = (max(0.0, float(telemetry.get("speed", 0.0) or 0.0))
                 if telemetry_fresh else 0.0)
        if vehicle_effects:
            g_x, g_y, g_z = tuple(v / self.GRAVITY
                                  for v in self.conditioned_acceleration)
        else:
            g_x, g_y, g_z = (0.0, 0.0, 0.0)
        self.vertical_slow += ((g_y - self.vertical_slow)
                               * self._alpha(1.8, elapsed))
        self.impact *= math.exp(-9.0 * elapsed)
        self.phase += elapsed
        translation = (0.0, 0.0, 0.0)
        rotation = (0.0, 0.0, 0.0)
        enabled, intensity, _ = self._effect("g_force")
        if vehicle_effects and enabled:
            translation = self._add(
                translation,
                (-g_x * 0.03, -g_y * 0.024, -g_z * 0.04), intensity)
            rotation = self._add(
                rotation, (g_z * 1.6, 0.0, -g_x * 2.0), intensity)
        enabled, intensity, _ = self._effect("impacts")
        if vehicle_effects and enabled and self.impact > 1e-05:
            wave = math.sin(self.phase * 52.0) * self.impact * intensity
            translation = self._add(
                translation, (0.0, wave * 0.028, -wave * 0.012))
            rotation = self._add(rotation, (wave * 0.9, 0.0, 0.0))
        enabled, intensity, _ = self._effect("road")
        if vehicle_effects and enabled:
            high = max(-2.0, min(2.0, g_y - self.vertical_slow))
            speed_factor = max(0.0, min(1.0, speed / 35.0))
            wave = math.sin(self.phase * (22.0 + speed_factor * 18.0))
            translation = self._add(translation, (
                wave * speed_factor * 0.0012,
                -g_y * 0.01 - high * 0.036 + wave * speed_factor * 0.0028,
                0.0), intensity)
        enabled, intensity, speed_scale = self._effect("breathing")
        if enabled:
            wave = math.sin(self.phase * 1.45 * speed_scale) * intensity
            translation = self._add(translation, (0.0, wave * 0.0015, 0.0))
            rotation = self._add(rotation, (wave * 0.08, 0.0, 0.0))
        enabled, intensity, speed_scale = self._effect("tremor")
        if enabled:
            a = (math.sin(self.phase * 43.0 * speed_scale)
                 + 0.3 * math.sin(self.phase * 91.0 * speed_scale))
            b = math.cos(self.phase * 53.0 * speed_scale)
            translation = self._add(
                translation, (a * 0.0008, b * 0.0006, 0.0), intensity)
            rotation = self._add(rotation, (0.0, 0.0, a * 0.1), intensity)
        enabled, intensity, speed_scale = self._effect("sway")
        if enabled:
            intensity *= (0.65 if free_camera else 1.0)
            a = math.sin(self.phase * 0.83 * speed_scale)
            b = math.sin(self.phase * 0.47 * speed_scale + 1.2)
            translation = self._add(
                translation, (a * 0.004, b * 0.002, 0.0), intensity)
            rotation = self._add(
                rotation, (b * 0.18, a * 0.16, a * 0.12), intensity)
        gain = max(0.0, min(2.0, float(self.settings.get("global_gain", 1.0))))
        translation = tuple(max(-0.12, min(0.12, v * gain))
                            for v in translation)
        rotation = tuple(max(-8.0, min(8.0, v * gain)) for v in rotation)
        return (translation, rotation)


def apply_fx(output, offset):
    translation, rotation = offset
    if translation == (0.0, 0.0, 0.0) and rotation == (0.0, 0.0, 0.0):
        return output
    xyz = tuple(a + b for a, b in zip(output["xyz"], translation))
    quaternion = output["quaternion"]
    if rotation != (0.0, 0.0, 0.0):
        quaternion = q_norm(q_mul(quaternion, q_from_euler_yxz_deg(*rotation)))
    return dict(output, xyz=xyz, quaternion=quaternion)


def translate(car, rotation, reset, fov, movement, options,
              free_spawn_offset, car_anchor, position=(0.0, 0.0, 0.0)):
    if not car and abs(free_spawn_offset[0]) >= 0.05:
        inward_yaw = -math.copysign(12.0, free_spawn_offset[0])
        rotation = q_norm(q_mul(rotation,
                                q_from_euler_yxz_deg(0.0, inward_yaw, 0.0)))
    anchor = car_anchor if car else free_spawn_offset
    return {
        "mode": 0 if car else 1,
        "xyz": (position[0] + anchor[0], position[1] + anchor[1],
                -position[2] + anchor[2]),
        "quaternion": rotation,
        "recenter": reset,
        "fov": fov,
        "move": (0.0, 0.0, 0.0) if car else (-movement[0], movement[1],
                                              movement[2]),
        "options": options & 12 if car else options,
    }
