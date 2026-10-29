from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent

WEB_PORT = 8787
MEDIAMTX_RTSP_PORT = 8654
MEDIAMTX_WEBRTC_PORT = 8989
MEDIAMTX_WEBRTC_UDP = 8289
MEDIAMTX_API_PORT = 9897
STREAM_PATH = "fh6"

CAPTURE_FPS = 60
CAPTURE_BITRATE = "12M"
CAPTURE_BITRATE_BPS = 12_000_000

PROJECT_ROOT = resource_root()
VENDOR = PROJECT_ROOT / "vendor"
WEB_DIR = PROJECT_ROOT / "web"
ASSETS_DIR = PROJECT_ROOT / "assets"


def realcam_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "RealCam"


def mediamtx_exe() -> Path:
    return VENDOR / "mediamtx" / "mediamtx.exe"


def find_ffmpeg() -> str | None:
    candidates = [
        VENDOR / "ffmpeg" / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "RealCam"
        / "Runtime" / "ffmpeg" / "ffmpeg.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("ffmpeg")


def find_ffprobe() -> str | None:
    c = VENDOR / "ffmpeg" / "ffprobe.exe"
    if c.is_file():
        return str(c)
    return shutil.which("ffprobe")


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _videos_folder() -> Path:
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes
            folder_id = uuid.UUID("18989B1D-99B5-455B-841C-AB7C74E4DDFC")
            guid = (ctypes.c_byte * 16).from_buffer_copy(folder_id.bytes_le)
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path:
                    return Path(path)
        except Exception:
            pass
    return Path.home() / "Videos"


def recordings_dir() -> Path:
    d = _videos_folder() / "HorizonLens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    d = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "HorizonLens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_virtual_ip(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if a in ipaddress.ip_network("100.64.0.0/10"):
        return True
    if a in ipaddress.ip_network("192.168.56.0/24"):
        return True
    return False


def lan_ipv4s() -> list[str]:
    import socket as _s
    import ipaddress

    real: list[str] = []
    virtual: list[str] = []

    def add(ip: str):
        if not ip:
            return
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return
        if a.version != 4 or a.is_loopback or a.is_link_local \
           or a.is_multicast or a.is_unspecified:
            return
        if ip in real or ip in virtual:
            return
        (virtual if _is_virtual_ip(ip) else real).append(ip)

    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in _s.getaddrinfo(_s.gethostname(), None, _s.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return real + virtual


def primary_lan_ip() -> str:
    ips = lan_ipv4s()
    return ips[0] if ips else "127.0.0.1"
