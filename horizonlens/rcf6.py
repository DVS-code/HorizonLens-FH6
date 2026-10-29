from __future__ import annotations

import glob
import hmac
import math
import os
import json
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

MAGIC = b"RCF6"
WIRE = struct.Struct("<4s4B3I11f")
BODY_LEN = 64
TAG_LEN = 16
PACKET_LEN = BODY_LEN + TAG_LEN
KEY_LEN = 32

COMPANION_ADDR = ("127.0.0.1", 9257)
SESSION_MAGIC = "REALCAM_FORZA_SESSION_V1"
SESSION_FILENAME = "RealCamForza6.session"

KIND_DRIVER = 1
KIND_FOLLOW_LOW = 2
KIND_FOLLOW_FAR = 3
KIND_DRIVER_NO_WHEEL = 4
KIND_COPTER = 5
KIND_FREECAM = 6
KIND_UNKNOWN = 7


def tag(body: bytes, key: bytes) -> bytes:
    if len(key) != KEY_LEN:
        raise ValueError("session key must be 32 bytes")
    return hmac.new(key, body, "sha256").digest()[:TAG_LEN]


AUTHENTICATED_VERSION = 2


def seal(body: bytes, key: bytes) -> bytes:
    if len(body) != BODY_LEN:
        raise ValueError(f"RCF6 body must be {BODY_LEN} bytes, got {len(body)}")
    if body[:4] == MAGIC:
        body = body[:4] + bytes((AUTHENTICATED_VERSION,)) + body[5:]
    return body + tag(body, key)


def euler_to_quaternion(pitch_deg: float, yaw_deg: float, roll_deg: float):
    x = math.radians(pitch_deg) / 2
    y = math.radians(yaw_deg) / 2
    z = math.radians(roll_deg) / 2
    sx, cx = math.sin(x), math.cos(x)
    sy, cy = math.sin(y), math.cos(y)
    sz, cz = math.sin(z), math.cos(z)
    return (
        cy * sx * cz + sy * cx * sz,
        sy * cx * cz - cy * sx * sz,
        cy * cx * sz - sy * sx * cz,
        cy * cx * cz + sy * sx * sz,
    )


@dataclass
class Pose:
    kind: int = KIND_DRIVER
    mode: int = 0
    options: int = 0
    session: int = 0
    sequence: int = 0
    recenter: int = 0
    xyz: tuple = (0.0, 0.0, 0.0)
    quat: tuple = (0.0, 0.0, 0.0, 1.0)
    fov: float = 60.0
    move: tuple = (0.0, 0.0, 0.0)

    def to_body(self) -> bytes:
        qx, qy, qz, qw = self.quat
        x, y, z = self.xyz
        mx, my, mz = self.move
        return WIRE.pack(
            MAGIC, 1, self.kind & 0xFF, self.mode & 0xFF, self.options & 0xFF,
            self.session & 0xFFFFFFFF, self.sequence & 0xFFFFFFFF,
            self.recenter & 0xFFFFFFFF,
            x, y, z, qx, qy, qz, qw, float(self.fov), mx, my, mz,
        )

    def seal(self, key: bytes) -> bytes:
        return seal(self.to_body(), key)


def parse_session_file(path: str):
    try:
        with open(path, "r", encoding="ascii", errors="strict") as fh:
            parts = fh.read().split()
    except OSError:
        return None
    if len(parts) != 4 or parts[0] != SESSION_MAGIC:
        return None
    try:
        pid = int(parts[1], 10)
        image_base = int(parts[2], 16)
        key = bytes.fromhex(parts[3])
    except ValueError:
        return None
    if len(key) != KEY_LEN:
        return None
    return pid, image_base, key


def find_session_files(sessions_root: str):
    pat = os.path.join(sessions_root, "**", SESSION_FILENAME)
    files = glob.glob(pat, recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files


def default_sessions_roots():
    la = os.environ.get("LOCALAPPDATA", "")
    roots = []
    pkg = os.path.join(
        la, "Programs", "RealCam", "Adapters", "Forza", "Package", "sessions"
    )
    roots.append(pkg)
    roots.append(os.path.join(la, "RealCam", "forza", "sessions"))
    return [r for r in roots if r]


def status_probe_body(session: int = 0, sequence: int = 1) -> bytes:
    return Pose(
        kind=KIND_UNKNOWN, mode=0, options=0, session=session,
        sequence=sequence, recenter=0, xyz=(0.0, 0.0, 0.0),
        quat=(0.0, 0.0, 0.0, 1.0), fov=60.0, move=(0.0, 0.0, 0.0),
    ).to_body()


def discover_session_key(sessions_root: str | None = None, timeout: float = 0.5):
    roots = [sessions_root] if sessions_root else default_sessions_roots()
    for root in roots:
        for path in find_session_files(root):
            parsed = parse_session_file(path)
            if parsed is None:
                continue
            pid, image_base, key = parsed
            if _pid_alive(pid):
                return key, pid, image_base, path
    return None


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return True


class CompanionLink:

    def __init__(self, key: bytes, addr=COMPANION_ADDR):
        if len(key) != KEY_LEN:
            raise ValueError("session key must be 32 bytes")
        self._key = key
        self._addr = addr
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0
        self.session = secrets.randbelow(4294967294) + 1
        self._probed = False
        self._seq_lock = threading.Lock()

    def next_sequence(self) -> int:
        with self._seq_lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            if self._seq == 0:
                self._seq = 1
            return self._seq

    def probe(self) -> bool:
        probe = Pose(kind=3, session=self.session, sequence=self.next_sequence())
        try:
            self._sock.sendto(probe.seal(self._key), self._addr)
            self._sock.settimeout(0.6)
            try:
                self._sock.recvfrom(65535)
                return True
            except socket.timeout:
                return False
        except OSError:
            return False
        finally:
            try:
                self._sock.settimeout(None)
            except OSError:
                pass

    def status(self, timeout: float = 0.8) -> dict | None:
        probe = Pose(kind=3, session=self.session, sequence=self.next_sequence())
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(timeout)
            sock.sendto(probe.seal(self._key), self._addr)
            data, _ = sock.recvfrom(65535)
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None
        finally:
            sock.close()

    def select_camera(self, command_mode: int) -> None:
        pose = Pose(kind=4, mode=command_mode, session=self.session,
                    sequence=self.next_sequence(),
                    quat=(0.0, 0.0, 0.0, 1.0), fov=60.0)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(pose.seal(self._key), self._addr)
        except OSError:
            pass
        finally:
            sock.close()

    def restore_camera(self) -> None:
        pose = Pose(kind=5, session=self.session,
                    sequence=self.next_sequence(),
                    quat=(0.0, 0.0, 0.0, 1.0), fov=60.0)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(pose.seal(self._key), self._addr)
        except OSError:
            pass
        finally:
            sock.close()

    def send_pose(self, pose: Pose) -> int:
        if not self._probed:
            self._probed = True
            self.probe()
        if pose.session == 0:
            pose.session = self.session
        if pose.sequence == 0:
            pose.sequence = self.next_sequence()
        datagram = pose.seal(self._key)
        return self._sock.sendto(datagram, self._addr)

    def close(self):
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
