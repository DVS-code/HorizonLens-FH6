from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from . import game

ACTIONS = [
    ("toggledoorLF", "Front L door", "panels"),
    ("toggledoorRF", "Front R door", "panels"),
    ("toggledoorLR", "Rear L door", "panels"),
    ("toggledoorRR", "Rear R door", "panels"),
    ("togglehood", "Hood", "panels"),
    ("toggletrunk", "Trunk", "panels"),
    ("toggleroof", "Roof", "panels"),
    ("togglestorage", "Storage", "panels"),
    ("toggleaero", "Active aero", "panels"),
    ("toggleheadlights", "Pop-up lights", "panels"),
    ("togglevents", "Vents", "panels"),
    ("togglewindowLF", "Front L window", "windows"),
    ("togglewindowRF", "Front R window", "windows"),
    ("togglewindowLR", "Rear L window", "windows"),
    ("togglewindowRR", "Rear R window", "windows"),
    ("toggleall", "Explode", "car"),
    ("resetstate", "Reset", "car"),
    ("maxdetail", "Max detail", "car"),
    ("toggleengine", "Ignition", "car"),
    ("lightsdrl", "Front DRL", "lights"),
    ("lightsrear", "Rear DRL", "lights"),
    ("lightsheadlights", "Headlights", "lights"),
    ("lightsall", "All lights", "lights"),
]
ACTION_IDS = {a for a, _, _ in ACTIONS}
ACTION_LABELS = {a: label for a, label, _ in ACTIONS}
ONE_SHOT_ACTIONS = {"resetstate"}

_VK = {f"F{n}": 0x6F + n for n in range(1, 25)}
_VK.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
_VK.update({f"D{n}": 0x30 + n for n in range(10)})
_VK.update({f"NumPad{n}": 0x60 + n for n in range(10)})
_VK.update({
    "Space": 0x20, "Tab": 0x09, "Return": 0x0D, "Enter": 0x0D, "Escape": 0x1B,
    "Back": 0x08, "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "Prior": 0x21, "PageDown": 0x22, "Next": 0x22,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "Multiply": 0x6A, "Add": 0x6B, "Subtract": 0x6D, "Decimal": 0x6E, "Divide": 0x6F,
    "Pause": 0x13, "Scroll": 0x91, "CapsLock": 0x14, "Capital": 0x14,
    "OemPlus": 0xBB, "OemComma": 0xBC, "OemMinus": 0xBD, "OemPeriod": 0xBE,
    "Oem2": 0xBF, "OemQuestion": 0xBF, "Oem3": 0xC0, "OemTilde": 0xC0,
    "Oem4": 0xDB, "OemOpenBrackets": 0xDB, "Oem5": 0xDC, "OemPipe": 0xDC,
    "Oem6": 0xDD, "OemCloseBrackets": 0xDD, "Oem7": 0xDE, "OemQuotes": 0xDE,
    "Oem1": 0xBA, "OemSemicolon": 0xBA, "OemBackslash": 0xE2, "Oem102": 0xE2,
})
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x6F}
_MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10}

_SETUP_KEYS = [f"F{n}" for n in range(13, 25)] + [f"Ctrl+F{n}" for n in range(13, 25)] \
    + [f"Shift+F{n}" for n in range(13, 25)]


def bindings_path() -> Path:
    env = os.environ.get("FORZAVISTA_BINDINGS_DIR", "")
    base = Path(env) if env and Path(env).is_absolute() else \
        Path(os.environ.get("APPDATA", str(Path.home()))) / "ForzavistaFreeRoam"
    return base / "hotkeys.json"


def parse_gesture(text: str):
    parts = [p.strip() for p in str(text).split("+") if p.strip()]
    if not parts:
        return None
    mods = []
    for p in parts[:-1]:
        vk = _MODS.get(p.lower())
        if vk is None:
            return None
        mods.append(vk)
    key = _VK.get(parts[-1])
    return (mods, key) if key else None


def load_bindings() -> dict:
    try:
        data = json.loads(bindings_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in ACTION_IDS and isinstance(v, str)}


def forzavista_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000).stdout.lower()
    except Exception:
        return False
    running = any(line.split(",")[0].strip('"').startswith(("forzavista", "forzavistafreeroam"))
                  for line in out.splitlines())
    _running_cache[:] = [time.monotonic(), running]
    return running


_running_cache = [-1e9, False]


def forzavista_running_cached(max_age: float = 3.0) -> bool:
    if time.monotonic() - _running_cache[0] > max_age:
        return forzavista_running()
    return _running_cache[1]


_bind_cache = [None, {}]


def load_bindings_cached() -> dict:
    try:
        sig = bindings_path().stat().st_mtime_ns
    except OSError:
        sig = None
    if sig != _bind_cache[0]:
        _bind_cache[:] = [sig, load_bindings()]
    return _bind_cache[1]


_u32 = ctypes.WinDLL("user32", use_last_error=True)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]


INJECT_MARK = 0x484C4E53
last_injected = [0.0]


def _key(vk: int, up: bool) -> _INPUT:
    flags = (0x0002 if up else 0) | (0x0001 if vk in _EXTENDED else 0)
    i = _INPUT(type=1)
    i.ki = _KEYBDINPUT(wVk=vk, wScan=_u32.MapVirtualKeyW(vk, 0), dwFlags=flags,
                       dwExtraInfo=INJECT_MARK)
    return i


def game_in_front() -> bool:
    pid = game.game_pid_cached()
    hwnd = _u32.GetForegroundWindow()
    if not pid or not hwnd:
        return False
    owner = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return owner.value == pid


def type_gesture(mods, vk) -> bool:
    downs = [_key(m, False) for m in mods] + [_key(vk, False)]
    ups = [_key(vk, True)] + [_key(m, True) for m in reversed(mods)]
    last_injected[0] = time.monotonic()
    arr = (_INPUT * len(downs))(*downs)
    ok = _u32.SendInput(len(downs), arr, ctypes.sizeof(_INPUT)) == len(downs)
    time.sleep(0.04)
    arr = (_INPUT * len(ups))(*ups)
    ok = _u32.SendInput(len(ups), arr, ctypes.sizeof(_INPUT)) == len(ups) and ok
    last_injected[0] = time.monotonic()
    return ok


def status() -> dict:
    binds = load_bindings()
    return {
        "running": forzavista_running(),
        "file": bindings_path().is_file(),
        "actions": [{"id": a, "label": label, "group": group,
                     "key": binds.get(a), "ok": bool(binds.get(a) and parse_gesture(binds[a]))}
                    for a, label, group in ACTIONS],
    }


def press(action: str, fast: bool = False) -> tuple[bool, str]:
    if action not in ACTION_IDS:
        return False, "unknown action"
    gesture = (load_bindings_cached() if fast else load_bindings()).get(action)
    if not gesture:
        return False, f"{ACTION_LABELS[action]} isn't bound in Forzavista"
    parsed = parse_gesture(gesture)
    if not parsed:
        return False, f"key {gesture} can't be sent; rebind it in Forzavista"
    if not (forzavista_running_cached() if fast else forzavista_running()):
        return False, "Forzavista isn't running"
    if not game_in_front():
        return False, "Forza must be the active window on the PC"
    return (True, "sent") if type_gesture(*parsed) else (False, "Windows blocked the key press")


def setup_bindings() -> tuple[bool, str]:
    if forzavista_running():
        return False, "Close Forzavista first, then try again (it rewrites this file)"
    path = bindings_path()
    try:
        current = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    used = {str(v).replace(" ", "").lower() for v in current.values()}
    free = [k for k in _SETUP_KEYS if k.lower() not in used]
    added = 0
    for action, _, _ in ACTIONS:
        if action in current or not free:
            continue
        current[action] = free.pop(0)
        added += 1
    if not added:
        return True, "Every action already has a key"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            shutil.copy2(path, path.with_suffix(".json.bak"))
        path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except OSError as error:
        return False, f"Couldn't write Forzavista's bindings: {error}"
    return True, f"Bound {added} action{'s' if added != 1 else ''}. Start Forzavista now."
