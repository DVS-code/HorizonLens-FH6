from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import secrets
import shutil
import time
from pathlib import Path

from . import config, game, camera_scan

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                               wt.DWORD, wt.DWORD]
k32.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
k32.WriteProcessMemory.restype = wt.BOOL
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wt.HMODULE, wt.LPCSTR]
k32.CreateRemoteThread.restype = wt.HANDLE
k32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                                   ctypes.POINTER(wt.DWORD)]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.CloseHandle.argtypes = [wt.HANDLE]

PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
INJECT_ACCESS = (PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                 PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ)

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

SESSION_MAGIC = "REALCAM_FORZA_SESSION_V1"
SESSION_FILE = "RealCamForza6.session"
INSTANCE_FILE = "RealCamForza6.instance"
DLL_NAME = "HorizonLensCamera.dll"
REALCAM_DLL_NAME = "RealCamForza6.dll"


def default_dll_source(edition: str = game.STEAM) -> Path | None:
    realcam = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "RealCam"
               / "Adapters" / "Forza" / "Package" / "companion")
    if edition == game.STORE:
        candidates = [config.VENDOR / "companion" / "xbox" / DLL_NAME,
                      realcam / REALCAM_DLL_NAME]
    else:
        candidates = [config.VENDOR / "companion" / DLL_NAME,
                      realcam / "steam" / REALCAM_DLL_NAME]
    for c in candidates:
        if c.is_file():
            return c
    return None


def session_dir() -> Path:
    import os
    base = Path(os.environ.get("LOCALAPPDATA", config.app_dir())) / "HorizonLens"
    d = base / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    k32.CloseHandle(h)
    return True


def _cleanup_stale_sessions(keep_pid: int):
    root = session_dir()
    for child in root.iterdir() if root.exists() else []:
        if not child.is_dir():
            continue
        try:
            cpid = int(child.name)
        except ValueError:
            continue
        if cpid == keep_pid or _pid_alive(cpid):
            continue
        try:
            shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


class AttachResult:
    def __init__(self, ok: bool, message: str, key: bytes | None = None,
                 pid: int | None = None, image_base: int | None = None):
        self.ok = ok
        self.message = message
        self.key = key
        self.pid = pid
        self.image_base = image_base


def _write_session_file(folder: Path, pid: int, image_base: int) -> bytes:
    key = secrets.token_bytes(32)
    line = f"{SESSION_MAGIC} {pid} {image_base:x} {key.hex()}"
    (folder / SESSION_FILE).write_text(line, encoding="ascii")
    return key


def _write_instance_file(folder: Path, pid: int, object_addr: int, image_base: int):
    line = f"{pid} {object_addr:x} {image_base:x}\n"
    (folder / INSTANCE_FILE).write_text(line, encoding="ascii")


def _remote_load_library(pid: int) -> int | None:
    k32.GetModuleHandleExW.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.HMODULE)]
    k32.GetModuleHandleExW.restype = wt.BOOL
    k32.GetModuleFileNameW.argtypes = [wt.HMODULE, wt.LPWSTR, wt.DWORD]
    k32.GetModuleFileNameW.restype = wt.DWORD
    local = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
    owner = wt.HMODULE()
    if not local or not k32.GetModuleHandleExW(6, ctypes.c_void_p(local), ctypes.byref(owner)):
        return None
    name = ctypes.create_unicode_buffer(32768)
    if not k32.GetModuleFileNameW(owner, name, len(name)):
        return None
    owner_name = Path(name.value).name.lower()
    remote = [base for module, base in _game_modules(pid) if module == owner_name]
    if len(remote) != 1:
        return None
    return remote[0] + local - owner.value


def _game_modules(pid: int) -> list[tuple[str, int]]:
    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
            ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
            ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
            ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
        ]
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    out = []
    for _ in range(20):
        snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
        if snap and snap != ctypes.c_void_p(-1).value:
            break
        if ctypes.get_last_error() != 24:
            return out
        time.sleep(0.01)
    else:
        return out
    try:
        me = MODULEENTRY32W(); me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            out.append((me.szModule.lower(), me.modBaseAddr or 0))
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return out


def _inject(pid: int, dll_path: str) -> tuple[bool, str]:
    h = k32.OpenProcess(INJECT_ACCESS, False, pid)
    if not h:
        return False, f"OpenProcess failed (err {ctypes.get_last_error()})"
    try:
        path_bytes = (dll_path + "\0").encode("utf-16-le")
        size = len(path_bytes)
        remote = k32.VirtualAllocEx(h, None, size, MEM_COMMIT | MEM_RESERVE,
                                    PAGE_READWRITE)
        if not remote:
            return False, f"VirtualAllocEx failed (err {ctypes.get_last_error()})"
        written = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(h, ctypes.c_void_p(remote), path_bytes, size,
                                    ctypes.byref(written))
        if not ok:
            k32.VirtualFreeEx(h, ctypes.c_void_p(remote), 0, MEM_RELEASE)
            return False, f"WriteProcessMemory failed (err {ctypes.get_last_error()})"

        load_lib = _remote_load_library(pid)
        if not load_lib:
            k32.VirtualFreeEx(h, ctypes.c_void_p(remote), 0, MEM_RELEASE)
            return False, "LoadLibraryW could not be resolved in the game"

        thread = k32.CreateRemoteThread(
            h, None, 0, ctypes.c_void_p(load_lib), ctypes.c_void_p(remote), 0, None)
        if not thread:
            k32.VirtualFreeEx(h, ctypes.c_void_p(remote), 0, MEM_RELEASE)
            return False, f"CreateRemoteThread failed (err {ctypes.get_last_error()})"

        k32.WaitForSingleObject(thread, 10000)
        exit_code = wt.DWORD(0)
        k32.GetExitCodeThread(thread, ctypes.byref(exit_code))
        k32.CloseHandle(thread)
        if exit_code.value == 0 and not _dll_already_loaded(pid):
            return False, ("LoadLibraryW returned 0 — the DLL failed to load "
                           "(wrong build, or its validation rejected the game).")
        return True, "injected"
    finally:
        k32.CloseHandle(h)


def _dll_already_loaded(pid: int) -> bool:
    try:
        import ctypes
        import ctypes.wintypes as wt
        TH32CS_SNAPMODULE = 0x08
        TH32CS_SNAPMODULE32 = 0x10

        class MODULEENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
                ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
            ]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return False
        try:
            me = MODULEENTRY32W(); me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32FirstW(snap, ctypes.byref(me))
            while ok:
                if me.szModule.lower() in (DLL_NAME.lower(), REALCAM_DLL_NAME.lower()):
                    return True
                ok = k32.Module32NextW(snap, ctypes.byref(me))
            return False
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return False


FOV_TABLE_RANGE = (20.0, 110.0)


def fov_table_targets(dll_path: Path) -> list[tuple[int, str]]:
    import struct
    d = Path(dll_path).read_bytes()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    count = struct.unpack_from("<H", d, pe + 6)[0]
    opt = struct.unpack_from("<H", d, pe + 20)[0]
    image_base = struct.unpack_from("<Q", d, pe + 24 + 24)[0]
    sections = [struct.unpack_from("<8sIIII", d, pe + 24 + opt + 40 * i) for i in range(count)]

    def rva_of(offset):
        for _, _, va, raw_size, raw in sections:
            if raw <= offset < raw + raw_size:
                return offset - raw + va
        raise ValueError("offset outside sections")

    name_at = d.find(b"\0chase_min\0") + 1
    if name_at <= 0:
        return []
    entry = d.find(struct.pack("<Q", image_base + rva_of(name_at))) - 16
    if entry < 0:
        return []
    out = []
    for i in range(6):
        rva, _, name_ptr, _ = struct.unpack_from("<QQQQ", d, entry + 32 * i)
        o = name_ptr - image_base
        for _, _, va, raw_size, raw in sections:
            if va <= o < va + raw_size:
                o = o - va + raw
                break
        out.append((rva, d[o:d.index(b"\0", o)].decode(errors="replace")))
    return out


def repair_fov_table(pid: int, image_base: int, dll_path: Path) -> list[str]:
    import math
    import struct
    try:
        targets = fov_table_targets(dll_path)
    except (OSError, ValueError, struct.error) as e:
        return [f"FOV table: module unreadable ({e})"]
    if len(targets) != 6:
        return ["FOV table: not found in the camera module"]
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    h = k32.OpenProcess(access, False, pid)
    if not h:
        return [f"FOV table: OpenProcess failed (err {ctypes.get_last_error()})"]
    lo, hi = FOV_TABLE_RANGE
    lines = []
    try:
        values = []
        for rva, _ in targets:
            buf = ctypes.c_float()
            got = ctypes.c_size_t()
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(image_base + rva), ctypes.byref(buf),
                                         4, ctypes.byref(got)) or got.value != 4:
                return [f"FOV table: read failed at +{rva:#x}"]
            values.append(buf.value)
        fixed = [v if math.isfinite(v) and lo <= v <= hi
                 else (hi if math.isfinite(v) and v > hi else lo) for v in values]
        for i in (0, 2, 4):
            fixed[i] = min(fixed[i], fixed[i + 1])
        summary = ", ".join(f"{n}={v:.1f}" for (_, n), v in zip(targets, values))
        if fixed == values:
            return [f"FOV table ok: {summary}"]
        lines.append(f"FOV table out of range: {summary}")
        for (rva, name), old, new in zip(targets, values, fixed):
            if new == old:
                continue
            buf = ctypes.c_float(new)
            done = ctypes.c_size_t()
            ok = k32.WriteProcessMemory(h, ctypes.c_void_p(image_base + rva), ctypes.byref(buf),
                                        4, ctypes.byref(done))
            lines.append(f"  {name}: {old:.1f} -> {new:.1f} " + ("(fixed)" if ok and done.value == 4
                         else f"(write failed, err {ctypes.get_last_error()})"))
    finally:
        k32.CloseHandle(h)
    return lines


def not_driving_yet(pid, camera_obj) -> str | None:
    problem = camera_scan.still_ready(pid, camera_obj)
    if problem:
        return problem
    try:
        from . import carpose
        tracker = carpose.CarTracker()
        try:
            pose = tracker.pose()
        finally:
            tracker.close()
    except Exception as error:
        return f"car unreadable ({error!r})"
    return None if pose is not None else "no drivable car yet"


def attach(dll_source: str | Path | None = None,
           progress=lambda m: None) -> AttachResult:
    pid = game.game_pid()
    if not pid:
        return AttachResult(False, "Forza Horizon 6 is not running.")

    def stop(message):
        camera_scan._log([f"pid={pid}", f"ATTACH STOPPED: {message}"])
        return AttachResult(False, message)

    if _dll_already_loaded(pid):
        return stop("The camera module is already loaded in this game session. "
                    "If the camera isn't responding, restart Forza Horizon 6, "
                    "get into a driving scene, and press Take camera again.")

    progress("Reading game memory for the camera…")
    scan = camera_scan.find_camera(pid, progress=progress)
    if not scan.ok:
        return AttachResult(False, f"Camera scan failed: {scan.message}")

    edition = camera_scan.profile_edition(scan.cls) or game.running_edition()
    progress("Microsoft Store edition…" if edition == game.STORE else "Steam edition…")
    src = Path(dll_source) if dll_source else default_dll_source(edition)
    if not src or not src.is_file():
        return stop(f"The camera module for the {edition} edition is missing.")
    camera_scan._log([f"pid={pid}", f"injecting {edition} module {src}"])

    folder = session_dir() / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_sessions(pid)
    dll_dst = folder / DLL_NAME
    try:
        if not dll_dst.is_file():
            shutil.copy2(src, dll_dst)
    except OSError as e:
        return AttachResult(False, f"Could not copy the DLL: {e}")

    progress("Staging session + instance files…")
    key = _write_session_file(folder, pid, scan.image_base)
    _write_instance_file(folder, pid, scan.object_addr, scan.image_base)

    camera_scan._log([f"pid={pid}"] + repair_fov_table(pid, scan.image_base, src))

    not_ready = not_driving_yet(pid, scan.object_addr)
    if not_ready:
        camera_scan._log([f"pid={pid}", f"WAITING: not in a driving view yet ({not_ready})"])
        return AttachResult(False, "Waiting for a driving view (not in a menu, loading "
                                   "screen or garage) before taking the camera.")

    progress("Injecting the companion…")
    ok, msg = _inject(pid, str(dll_dst))
    if not ok:
        return stop(f"Loading the camera module failed: {msg}")

    log = folder / "RealCamForza6.log"
    installed = False
    detail = ""
    for _ in range(20):
        time.sleep(0.1)
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        if "INSTANCE_HOOK_INSTALLED" in text:
            installed = True
            break
        if "INSTANCE_REJECTED" in text or "CAMERA_SELECTOR_UNAVAILABLE" in text:
            for line in reversed(text.strip().splitlines()):
                if "REJECTED" in line or "UNAVAILABLE" in line:
                    detail = line.split("] ", 1)[-1]
                    break
            break

    if installed:
        camera_scan._log([f"pid={pid}", "ATTACHED: camera hook installed"])
        progress("Companion attached — camera hook installed.")
        return AttachResult(True, "Companion attached and camera hook installed.",
                            key=key, pid=pid, image_base=scan.image_base)
    try:
        tail = log.read_text(errors="replace").strip().splitlines()[-8:]
    except OSError:
        tail = ["(no module log)"]
    camera_scan._log([f"pid={pid}", "module log:"] + tail)
    return stop("The camera module loaded but refused the camera "
                f"({detail or 'no hook installed'}). Make sure you're in a "
                "normal driving view and try again.")
