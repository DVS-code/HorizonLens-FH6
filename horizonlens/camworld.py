from __future__ import annotations

import ctypes
import math
import struct
import time
from ctypes import wintypes

from . import companion, game, relay

MATRIX_OFFSET = 0x650

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.ReadProcessMemory.restype = wintypes.BOOL
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_vec(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def transpose(a):
    return [[a[j][i] for j in range(3)] for i in range(3)]


def det(a):
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def inverse(a):
    d = det(a)
    if abs(d) < 1e-9:
        return None
    c = [[(a[(j + 1) % 3][(i + 1) % 3] * a[(j + 2) % 3][(i + 2) % 3]
           - a[(j + 1) % 3][(i + 2) % 3] * a[(j + 2) % 3][(i + 1) % 3]) / d
          for j in range(3)] for i in range(3)]
    return c


def quat_to_mat(q):
    x, y, z, w = relay.q_norm(q)
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def mat_to_quat(m):
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = (0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s, (m[1][0] - m[0][1]) / s)
    return relay.q_norm(q)


def rot_error_deg(a, b):
    r = mat_mul(transpose(a), b)
    c = max(-1.0, min(1.0, (r[0][0] + r[1][1] + r[2][2] - 1.0) / 2.0))
    return math.degrees(math.acos(c))


class CameraReader:

    def __init__(self):
        self._pid = None
        self._h = None
        self._obj = None
        self._flip = None

    def _attach(self):
        pid = game.game_pid_cached()
        if not pid:
            self.close()
            return False
        if pid != self._pid or not self._h:
            self.close()
            try:
                line = (companion.session_dir() / str(pid) / companion.INSTANCE_FILE).read_text()
                self._obj = int(line.split()[1], 16)
            except (OSError, ValueError, IndexError):
                return False
            self._h = _k32.OpenProcess(0x0400 | 0x0010, False, pid)
            self._pid = pid if self._h else None
        return bool(self._h)

    def close(self):
        if self._h:
            _k32.CloseHandle(self._h)
        self._h = self._pid = self._obj = None

    def raw(self):
        if not self._attach():
            return None
        buf = ctypes.create_string_buffer(64)
        got = ctypes.c_size_t()
        if not _k32.ReadProcessMemory(self._h, ctypes.c_void_p(self._obj + MATRIX_OFFSET), buf, 64,
                                      ctypes.byref(got)) or got.value != 64:
            self.close()
            return None
        m = struct.unpack("<16f", buf.raw)
        if not all(math.isfinite(v) for v in m) or abs(m[15] - 1.0) > 0.05:
            return None
        return m

    def pose(self):
        m = self.raw()
        if m is None:
            return None
        A = [list(m[0:3]), list(m[4:7]), list(m[8:11])]
        t = m[12:15]
        pos = tuple(-sum(t[k] * A[j][k] for k in range(3)) for j in range(3))
        rot = [[A[j][i] for j in range(3)] for i in range(3)]
        if det(rot) < 0:
            rot[2] = [-v for v in rot[2]]
        return pos, rot

    def capture(self):
        got = self.pose()
        if got is None:
            return None
        pos, rot = got
        return {"pos": tuple(pos), "quat": mat_to_quat(rot)}


VMAX = 30.0


def _log(msg):
    try:
        from . import config
        path = config.data_dir() / "sequencer.log"
        if path.is_file() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".old.log"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def q_angle_deg(a, b):
    d = abs(sum(x * y for x, y in zip(relay.q_norm(a), relay.q_norm(b))))
    return math.degrees(2 * math.acos(min(1.0, d)))


def _v(v):
    return "(" + ", ".join(f"{c:.2f}" for c in v) + ")"


def look_angles(rot):
    f = rot[2]
    return (math.degrees(math.atan2(f[0], f[2])),
            math.degrees(math.asin(max(-1.0, min(1.0, f[1])))))


def wire_look(yaw, pitch, roll=0.0):
    return relay.q_norm(relay.q_from_euler_yxz_deg(-pitch, yaw, roll))


def wire_angles(quat):
    x, y, z, w = relay.q_norm(quat)
    pitch = -math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - z * y)))))
    yaw = math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (x * x + y * y)))
    roll = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (x * x + z * z)))
    return yaw, pitch, roll
