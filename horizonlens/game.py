from __future__ import annotations

import functools
import os
import subprocess
import sys
from pathlib import Path

FH6_STEAM_APPID = "2483190"
FH6_EXE_NAMES = ("forzahorizon6.exe",)
FH6_WINDOW_HINTS = ("Forza Horizon",)
STORE_PACKAGE = "Microsoft.ForteBaseGame"
STEAM = "steam"
STORE = "xbox"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0


def _matches_game(image_name: str) -> bool:
    flat = image_name.lower().replace(" ", "")
    return flat in FH6_EXE_NAMES or "forzahorizon6" in flat


def _run_pids_by_name(names) -> list[int]:
    wanted = {n.lower() for n in names}
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            TH32CS_SNAPPROCESS = 0x02

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260),
                ]
            k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
            k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            INVALID = ctypes.c_void_p(-1).value
            if snap and snap != INVALID:
                try:
                    pe = PROCESSENTRY32W(); pe.dwSize = ctypes.sizeof(pe)
                    pids = []
                    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
                    while ok:
                        if pe.szExeFile.lower() in wanted or _matches_game(pe.szExeFile):
                            pids.append(pe.th32ProcessID)
                        ok = k32.Process32NextW(snap, ctypes.byref(pe))
                    return pids
                finally:
                    k32.CloseHandle(snap)
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
            timeout=8, creationflags=0x08000000 if sys.platform == "win32" else 0)
        pids = []
        for line in out.stdout.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].strip('"').lower() in wanted:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return []


def game_pid() -> int | None:
    pids = _run_pids_by_name([n.lower() for n in FH6_EXE_NAMES])
    return pids[0] if pids else None


_pid_cache = (0.0, None)


def game_pid_cached(max_age: float = 1.0) -> int | None:
    global _pid_cache
    import time as _t
    now = _t.monotonic()
    if now - _pid_cache[0] > max_age:
        _pid_cache = (now, game_pid())
    return _pid_cache[1]


def is_running() -> bool:
    return game_pid() is not None


def image_path(pid: int) -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(h)


def edition_of_path(path: str | None) -> str:
    p = (path or "").lower()
    if STORE_PACKAGE.lower() + "_" in p or "\\windowsapps\\" in p \
            or "\\xboxgames\\" in p:
        return STORE
    steam = find_steam_install()
    if steam and p.startswith(str(steam.parent).lower()):
        return STEAM
    app = store_app()
    if app and app[0] and p.startswith(app[0].lower().rstrip("\\")):
        return STORE
    return STEAM


_IDENTITY: dict = {}


def identify_running(pid: int | None = None) -> dict:
    pid = pid or game_pid()
    if not pid:
        return {}
    if pid not in _IDENTITY:
        from . import camera_scan
        info = camera_scan.identify(pid)
        if info.get("edition") is None:
            info["edition"] = edition_of_path(info.get("path") or image_path(pid))
            info["edition_from"] = "folder"
        else:
            info["edition_from"] = "game version"
        if info.get("pe") is None and info.get("error"):
            return info
        _IDENTITY.clear()
        _IDENTITY[pid] = info
    return _IDENTITY[pid]


def running_edition() -> str | None:
    return identify_running().get("edition")


def _steam_root() -> Path | None:
    candidates = []
    if sys.platform == "win32":
        import winreg
        for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    candidates.append(Path(str(winreg.QueryValueEx(k, value)[0])))
            except OSError:
                pass
    candidates += [Path(os.environ.get("ProgramFiles(x86)", "")) / "Steam",
                   Path(os.environ.get("ProgramFiles", "")) / "Steam"]
    for p in candidates:
        if (p / "steam.exe").is_file():
            return p
    return None


def _steam_libraries() -> list[Path]:
    root = _steam_root()
    libs = []
    if root:
        libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                for line in vdf.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line.lower().startswith('"path"'):
                        parts = line.split('"')
                        if len(parts) >= 4:
                            libs.append(Path(parts[3].replace("\\\\", "\\")))
            except Exception:
                pass
    seen, out = set(), []
    for l in libs:
        s = str(l).lower()
        if s not in seen:
            seen.add(s); out.append(l)
    return out


def find_steam_install() -> Path | None:
    for lib in _steam_libraries():
        exe = lib / "steamapps" / "common" / "ForzaHorizon6" / "forzahorizon6.exe"
        if exe.is_file():
            return exe
    return None


def _powershell(script: str) -> str:
    try:
        r = subprocess.run(
            [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
             "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, creationflags=_NOWIN)
        return r.stdout.strip()
    except Exception:
        return ""


@functools.lru_cache(maxsize=1)
def store_app() -> tuple[str, str] | None:
    out = _powershell(
        f"$p = Get-AppxPackage -Name '{STORE_PACKAGE}' | Select-Object -First 1;"
        f"if ($p) {{ $a = Get-StartApps | Where-Object {{ $_.AppID -like "
        f"\"$($p.PackageFamilyName)!*\" }} | Select-Object -First 1;"
        f"$p.InstallLocation; $a.AppID }}")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if len(lines) >= 2:
        return lines[0], lines[1]
    if len(lines) == 1:
        return lines[0], ""
    return None


def installed_editions() -> list[str]:
    editions = []
    if find_steam_install():
        editions.append(STEAM)
    if store_app():
        editions.append(STORE)
    return editions


def find_install() -> Path | None:
    exe = find_steam_install()
    if exe:
        return exe
    app = store_app()
    return Path(app[0]) if app else None


def is_installed() -> bool:
    return find_install() is not None


def launch(edition: str | None = None) -> bool:
    if is_running():
        return True
    editions = installed_editions()
    edition = edition if edition in editions else (editions[0] if editions else STEAM)
    try:
        if edition == STORE:
            app = store_app()
            if app and app[1]:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app[1]}"],
                                 creationflags=_NOWIN)
                return True
            return False
        root = _steam_root()
        if root:
            subprocess.Popen([str(root / "steam.exe"), "-applaunch", FH6_STEAM_APPID],
                             creationflags=_NOWIN)
        else:
            os.startfile(f"steam://rungameid/{FH6_STEAM_APPID}")
        return True
    except Exception:
        return False


def main_window(pid: int | None = None):
    pid = pid or game_pid()
    if not pid or sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    best = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) \
                and not user32.IsIconic(hwnd):
            rect = wt.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w >= 320 and h >= 240:
                    best.append((w * h, hwnd, w, h))
        return True

    user32.EnumWindows(visit, 0)
    if not best:
        return None
    _, hwnd, w, h = max(best)
    return hwnd, w, h


def window_title() -> str | None:
    pid = game_pid()
    if not pid or sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        EnumWindows = user32.EnumWindows
        EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM),
                                wt.LPARAM]
        GetWindowThreadProcessId = user32.GetWindowThreadProcessId
        IsWindowVisible = user32.IsWindowVisible
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW

        result = {"title": None}

        def cb(hwnd, lparam):
            wpid = wt.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and IsWindowVisible(hwnd):
                n = GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value.strip():
                        result["title"] = buf.value
                        return False
            return True

        EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
        return result["title"]
    except Exception:
        return None


_cfg_cache: dict = {}


def _config_files() -> list[Path]:
    import time as _t
    now = _t.monotonic()
    if now - _cfg_cache.get("files_at", -1e9) > 30.0:
        _cfg_cache.update(files_at=now, files=_find_config_files())
    return [f for f in _cfg_cache["files"] if f.is_file()]


def _find_config_files() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    rel = Path("LocalStorage_Shared") / "ForzaUserConfigSelections" / "UserConfigSelections"
    files = [local / "ForzaHorizon6" / rel]
    packages = local / "Packages"
    try:
        for pkg in packages.glob(STORE_PACKAGE + "_*"):
            files += list(pkg.glob("**/ForzaUserConfigSelections/UserConfigSelections"))[:4]
    except OSError:
        pass
    return [f for f in files if f.is_file()]


def fullscreen_setting() -> bool | None:
    import re
    files = _config_files()
    if not files:
        return None
    try:
        newest = max(files, key=lambda f: f.stat().st_mtime)
        sig = (str(newest), newest.stat().st_mtime_ns)
    except OSError:
        return None
    if _cfg_cache.get("sig") != sig:
        value = None
        try:
            text = newest.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'<Fullscreen\s+value="(\d+)"', text)
            if m:
                value = m.group(1) != "0"
        except OSError:
            pass
        _cfg_cache.update(sig=sig, value=value)
    return _cfg_cache.get("value")


def exclusive_fullscreen_now() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value == 3
    except Exception:
        return False


_fs_seen = {"pid": None, "live": False}


def fullscreen() -> bool:
    pid = game_pid_cached()
    if pid != _fs_seen["pid"]:
        _fs_seen.update(pid=pid, live=False)
    if pid and exclusive_fullscreen_now():
        try:
            import ctypes
            import ctypes.wintypes as wt
            u32 = ctypes.windll.user32
            fg = wt.DWORD()
            u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(fg))
            if fg.value == pid:
                _fs_seen["live"] = True
        except Exception:
            pass
    return bool(fullscreen_setting()) or _fs_seen["live"]


def status() -> dict:
    pid = game_pid()
    install = find_install()
    return {
        "installed": install is not None,
        "install_path": str(install) if install else None,
        "editions": installed_editions(),
        "running": pid is not None,
        "pid": pid,
        "edition": identify_running(pid).get("edition") if pid else None,
    }
