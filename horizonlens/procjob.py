from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes

_job = None


def _kill_on_close_job():
    global _job
    if _job is not None or sys.platform != "win32":
        return _job
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD)

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in
                    ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.Basic.LimitFlags = 0x2000
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    _job = job
    return _job


def own(proc: subprocess.Popen) -> subprocess.Popen:
    try:
        job = _kill_on_close_job()
        if job:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
    except Exception:
        pass
    return proc


def clear_orphaned_mediamtx() -> list[int]:
    if sys.platform != "win32":
        return []
    script = ("Get-CimInstance Win32_Process -Filter \"Name='mediamtx.exe'\" | ForEach-Object {"
              " $p = Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue;"
              " \"$($_.ProcessId)|$($_.ExecutablePath)|$(if ($p) { $p.ProcessName } else { '' })\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=20,
                             creationflags=0x08000000).stdout
    except Exception:
        return []
    ended = []
    for line in out.splitlines():
        pid, _, rest = line.strip().partition("|")
        path, _, parent = rest.rpartition("|")
        ours = "\\_internal\\vendor\\mediamtx\\" in path.lower().replace("/", "\\") \
            or "\\horizonlens\\vendor\\mediamtx\\" in path.lower().replace("/", "\\")
        if not pid.isdigit() or not ours or parent:
            continue
        subprocess.run(["taskkill", "/pid", pid, "/f"], capture_output=True,
                       timeout=10, creationflags=0x08000000)
        ended.append(int(pid))
    return ended
