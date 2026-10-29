from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import time
import math

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.CloseHandle.argtypes = [wt.HANDLE]

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

MATRIX_OFFSET = 0x650

PROFILES = {
    (0x6ABA856E, 0x0B3AD000): ("Xbox3.461.691.0 (Microsoft Store)",
                               0x29C61B0, 0x64CBFE0, 0x78B3888),
    (0x6ABA8552, 0x0B3C0000): ("Steam2.461.691.0", 0x2A44890, 0x64DF160, 0x78B75B8),
    (0x6A995082, 0x0B3C4000): ("Steam2.440.853.0", 0x2A36790, 0x64C7A70, 0x78A3258),
}
GETTER_BYTES = bytes.fromhex(
    "0f108150060000488bc20f1089600600000f29020f1081700600000f294a10"
    "0f1089800600000f2942200f294a30c3")


CAMERA_VECTOR_OFFSET = 1336
CAMERA_ENTRY_SIZE = 72
CAMERA_ENTRY_OBJECT_OFFSET = 56
CAMERA_REGISTRY_VTABLE_OFFSET = 1328
SELECTOR_GRAPH_ENTRIES = ((0, "FollowCam"), (2, "DriverCam"), (20, "Copter"))
INLINE_CAMERA_NAME_CAPACITY = 15
MAX_CAMERA_NAME_LENGTH = 64
MIN_ENTRY_COUNT, MAX_ENTRY_COUNT = 20, 64
MANAGER_READ_SIZE = 1496


def _camera_entry_name(proc, entry: bytes):
    if len(entry) < 32:
        return None
    length, capacity = struct.unpack_from("<QQ", entry, 16)
    if not (0 < length <= MAX_CAMERA_NAME_LENGTH):
        return None
    if capacity < length:
        return None
    if capacity == INLINE_CAMERA_NAME_CAPACITY:
        raw = entry[:length]
    else:
        if not (INLINE_CAMERA_NAME_CAPACITY < capacity <= 0x1000):
            return None
        ptr = struct.unpack_from("<Q", entry, 0)[0]
        if ptr < 0x10000 or ptr > 0x7FFFFFFFFFFF:
            return None
        raw = proc.read(ptr, length)
    if not raw or len(raw) < length:
        return None
    try:
        return raw.split(b"\x00")[0].decode("ascii")
    except Exception:
        return None


def _selector_graph_problem(proc, obj) -> str | None:
    data = proc.read(obj, MANAGER_READ_SIZE)
    if not data or len(data) != MANAGER_READ_SIZE:
        return "manager unreadable"
    first, last = struct.unpack_from("<QQ", data, CAMERA_VECTOR_OFFSET)
    if not first or first % 8 != 0 or last < first:
        return "camera list empty"
    span = last - first
    if span % CAMERA_ENTRY_SIZE != 0:
        return f"camera list malformed ({span} bytes)"
    count = span // CAMERA_ENTRY_SIZE
    if not (MIN_ENTRY_COUNT <= count <= MAX_ENTRY_COUNT):
        return f"{count} cameras registered"
    entries = proc.read(first, count * CAMERA_ENTRY_SIZE)
    if not entries or len(entries) != count * CAMERA_ENTRY_SIZE:
        return "camera list unreadable"

    def name_at(index):
        return _camera_entry_name(
            proc, entries[index * CAMERA_ENTRY_SIZE:(index + 1) * CAMERA_ENTRY_SIZE])

    names = None
    for index, name in SELECTOR_GRAPH_ENTRIES:
        if index < count and name_at(index) == name:
            continue
        if names is None:
            names = [name_at(i) for i in range(count)]
        if name not in names:
            return f"{name} not registered ({count} cameras)"
    return None


def _selector_graph_ready(proc, obj) -> bool:
    return _selector_graph_problem(proc, obj) is None


def _candidate_problem(proc, obj, tries=3) -> str | None:
    problem = None
    for attempt in range(tries):
        mat = proc.read(obj + MATRIX_OFFSET, 64)
        if not mat or not _valid_matrix(mat):
            problem = "camera matrix not valid"
        else:
            problem = _selector_graph_problem(proc, obj)
            if problem is None:
                return None
        if attempt + 1 < tries:
            time.sleep(0.02)
    return problem


def still_ready(pid, obj) -> str | None:
    try:
        proc = _Proc(pid)
    except OSError as error:
        return str(error)
    try:
        return _candidate_problem(proc, obj)
    finally:
        proc.close()


class ScanResult:
    def __init__(self, ok, message, image_base=0, object_addr=0,
                 slot_addr=0, expected_value=0, vtable_rva=0, cls=""):
        self.ok = ok
        self.message = message
        self.image_base = image_base
        self.object_addr = object_addr
        self.slot_addr = slot_addr
        self.expected_value = expected_value
        self.vtable_rva = vtable_rva
        self.cls = cls


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__align", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class _Proc:
    def __init__(self, pid):
        self.h = k32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise OSError(f"OpenProcess failed (err {ctypes.get_last_error()})")

    def read(self, addr, size):
        if not 0 < size <= 4 * 1024 * 1024:
            raise ValueError("read must be bounded to 4 MiB")
        buf = (ctypes.c_char * size)()
        got = ctypes.c_size_t(0)
        k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size,
                              ctypes.byref(got))
        return bytes(buf[:got.value]) if got.value else None

    def regions(self, start=0, end=0x7FFFFFFFFFFF):
        addr = start
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < end:
            r = k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                   ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not r:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            yield base, size, mbi.State, mbi.Protect, mbi.Type
            if size == 0:
                break
            addr = base + size

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None


def _main_module_base(pid):
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
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap and snap != ctypes.c_void_p(-1).value:
        me = MODULEENTRY32W(); me.dwSize = ctypes.sizeof(me)
        first = None
        try:
            ok = k32.Module32FirstW(snap, ctypes.byref(me))
            while ok:
                if first is None and me.modBaseAddr:
                    first = (me.modBaseAddr, me.szExePath)
                if me.szModule.lower() == "forzahorizon6.exe" and me.modBaseAddr:
                    return me.modBaseAddr, me.szExePath
                ok = k32.Module32NextW(snap, ctypes.byref(me))
        finally:
            k32.CloseHandle(snap)
        if first:
            return first
    return _main_module_base_psapi(pid)


def _main_module_base_psapi(pid):
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p),
                                               wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
        psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                               wt.LPWSTR, wt.DWORD]
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return 0, None
        try:
            modules = (ctypes.c_void_p * 1)()
            needed = wt.DWORD(0)
            if not psapi.EnumProcessModulesEx(h, modules, ctypes.sizeof(modules),
                                              ctypes.byref(needed), 0x03):
                return 0, None
            name = ctypes.create_unicode_buffer(32768)
            psapi.GetModuleFileNameExW(h, modules[0], name, len(name))
            return (modules[0] or 0), name.value or None
        finally:
            k32.CloseHandle(h)
    except Exception:
        return 0, None


def _read_header(proc, base):
    hdr = proc.read(base, 0x10000)
    if not hdr or hdr[:2] != b"MZ":
        return None
    e_lfanew = struct.unpack_from("<I", hdr, 0x3C)[0]
    if e_lfanew + 24 + 60 > len(hdr) or hdr[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return None
    if struct.unpack_from("<H", hdr, e_lfanew + 4)[0] != 0x8664:
        return None
    time_stamp = struct.unpack_from("<I", hdr, e_lfanew + 8)[0]
    size_of_image = struct.unpack_from("<I", hdr, e_lfanew + 24 + 56)[0]
    return time_stamp, size_of_image


def _validate_profile(proc, base, profile):
    _, getter, vtable, locator = profile
    if proc.read(base + getter, len(GETTER_BYTES)) != GETTER_BYTES:
        return "camera getter bytes differ"
    if proc.read(base + vtable + 96, 8) != struct.pack("<Q", base + getter):
        return "camera getter slot differs, or another camera mod is already attached"
    if proc.read(base + vtable - 8, 8) != struct.pack("<Q", base + locator):
        return "camera type locator differs"
    col = proc.read(base + locator, 24)
    if not col or len(col) != 24:
        return "camera RTTI unreadable"
    col = struct.unpack("<6I", col)
    if col[0] != 1 or col[1] != 0 or col[5] != locator:
        return "camera RTTI self-reference differs"
    return None


def _valid_matrix(raw):
    if len(raw) < 64:
        return False
    m = struct.unpack("<16f", raw[:64])
    for v in m:
        if not math.isfinite(v):
            return False
    if abs(m[15] - 1.0) > 0.02:
        return False

    def unit(a, b, c):
        return abs((a * a + b * b + c * c) - 1.0) <= 0.04

    if not unit(m[0], m[1], m[2]) or abs(m[3]) > 0.02:
        return False
    if not unit(m[4], m[5], m[6]) or abs(m[7]) > 0.02:
        return False
    if not unit(m[8], m[9], m[10]) or abs(m[11]) > 0.02:
        return False
    if abs(m[12]) > 1e6 or abs(m[13]) > 1e6 or abs(m[14]) > 1e6:
        return False
    d01 = m[0]*m[4] + m[1]*m[5] + m[2]*m[6]
    d02 = m[0]*m[8] + m[1]*m[9] + m[2]*m[10]
    d12 = m[4]*m[8] + m[5]*m[9] + m[6]*m[10]
    if abs(d01) > 0.03 or abs(d02) > 0.03 or abs(d12) > 0.03:
        return False
    return True


def identify(pid) -> dict:
    info = {"path": None, "pe": None, "profile": None, "edition": None}
    try:
        proc = _Proc(pid)
    except OSError as e:
        info["error"] = str(e)
        return info
    try:
        base, info["path"] = _main_module_base(pid)
        if base:
            info["pe"] = _read_header(proc, base)
        profile = PROFILES.get(info["pe"]) if info["pe"] else None
        if profile:
            info["profile"] = profile[0]
            info["edition"] = "xbox" if profile[0].startswith("Xbox") else "steam"
        return info
    finally:
        proc.close()


def profile_edition(profile_name: str | None) -> str | None:
    if not profile_name:
        return None
    return "xbox" if profile_name.startswith("Xbox") else "steam"


HEAP_PROTECT = (0x04, 0x08, 0x40, 0x80)
MEM_PRIVATE = 0x20000
CHUNK = 4 * 1024 * 1024


def _log(lines):
    try:
        import os
        import time as _t
        from pathlib import Path
        d = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "HorizonLens"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "attach.log", "a", encoding="utf-8") as f:
            f.write(f"--- {_t.strftime('%Y-%m-%d %H:%M:%S')}\n")
            for line in lines:
                f.write(f"{line}\n")
    except Exception:
        pass


def find_camera(pid, time_budget: float = 75.0, progress=None) -> ScanResult:
    import time as _time
    diag = [f"pid={pid}"]

    def fail(message):
        diag.append(f"FAILED: {message}")
        _log(diag)
        return ScanResult(False, message)

    try:
        proc = _Proc(pid)
    except OSError as e:
        return fail(f"Can't open the game ({e}).")
    try:
        base, path = _main_module_base(pid)
        diag.append(f"module={path} base={base:#x}" if base else "module=not found")
        if not base:
            return fail("Could not find the game's main module.")
        meta = _read_header(proc, base)
        if not meta:
            return fail("Could not read the game's PE header.")
        time_stamp, size_of_image = meta
        diag.append(f"pe=({time_stamp:#x}, {size_of_image:#x})")
        profile = PROFILES.get((time_stamp, size_of_image))
        if profile is None:
            return fail(
                "This Forza Horizon 6 version isn't supported yet (PE %08x/%08x). "
                "Supported: Steam 2.461.691.0 / 2.440.853.0 and Microsoft Store "
                "3.461.691.0." % (time_stamp, size_of_image))
        diag.append(f"profile={profile[0]}")
        problem = _validate_profile(proc, base, profile)
        if problem:
            return fail(f"The game's camera code doesn't match {profile[0]}: {problem}.")
        needle = base + profile[2]
        needle_bytes = struct.pack("<Q", needle)

        regions = [(b, size) for b, size, state, protect, mtype in proc.regions()
                   if state == 0x1000 and mtype == MEM_PRIVATE
                   and (protect & 0xFF) in HEAP_PROTECT and not protect & 0x100]
        regions.sort(key=lambda r: r[1], reverse=True)
        total = sum(size for _, size in regions) or 1
        diag.append(f"heap regions={len(regions)} bytes={total}")
        deadline = _time.time() + (time_budget or 75.0)
        done = hits = 0
        candidates = []

        def found_at(obj, how):
            diag.append(f"camera={obj:#x} {how}")
            _log(diag)
            return ScanResult(True, f"Found the camera ({profile[0]})",
                              image_base=base, object_addr=obj, slot_addr=obj,
                              expected_value=needle, vtable_rva=profile[2],
                              cls=profile[0])

        def recheck(seconds=15.0):
            if not candidates:
                return None
            if progress:
                progress("Waiting for the game camera to be ready…")
            until = _time.time() + seconds
            last = {}
            while _time.time() < until:
                for obj in candidates:
                    last[obj] = _candidate_problem(proc, obj, tries=1)
                    if last[obj] is None:
                        return found_at(obj, "on recheck")
                _time.sleep(0.25)
            for obj, problem in last.items():
                diag.append(f"candidate {obj:#x} still rejected: {problem}")
            return None
        next_report = 256 * 1024 * 1024
        for b, size in regions:
            addr, end = b, b + size
            while addr < end:
                if _time.time() > deadline:
                    diag.append(f"scanned={done} vtable hits={hits}")
                    found = recheck()
                    if found:
                        return found
                    return fail("Camera scan timed out. Make sure you are in a drivable "
                                "scene (not a menu or loading), then try again.")
                length = min(CHUNK, end - addr)
                data = proc.read(addr, length)
                done += length
                if progress and done >= next_report:
                    next_report += 256 * 1024 * 1024
                    progress(f"Scanning game memory… {done // 1048576} / {total // 1048576} MB")
                if data:
                    j = data.find(needle_bytes)
                    while j >= 0:
                        if j % 8 == 0:
                            hits += 1
                            obj = addr + j
                            problem = _candidate_problem(proc, obj)
                            if problem is None:
                                return found_at(obj, f"after {done} bytes, {hits} hits")
                            if len(candidates) < 16:
                                candidates.append(obj)
                                diag.append(f"candidate {obj:#x} rejected: {problem}")
                        j = data.find(needle_bytes, j + 1)
                addr += length
        diag.append(f"scanned={done} vtable hits={hits}")
        found = recheck()
        if found:
            return found
        return fail("No live camera found. Get into free roam behind the wheel "
                    "(not a menu or loading), then press Take camera again.")
    finally:
        proc.close()
