from __future__ import annotations

import asyncio
import json
import platform
import ssl
import subprocess
import sys
import time
import traceback

from . import __version__, config, game, streaming


def _check(results, name, fn):
    try:
        ok, detail = fn()
    except Exception as error:
        ok, detail = False, f"{type(error).__name__}: {error}"
    results.append((ok, name, detail))


def _game():
    installed = ", ".join(game.installed_editions()) or "none found"
    if not game.is_running():
        return game.is_installed(), f"installed: {installed}; not running"
    info = game.identify_running()
    pe = info.get("pe")
    pe_text = f"PE {pe[0]:08x}/{pe[1]:08x}" if pe else "PE unreadable"
    edition = {"xbox": "Microsoft Store", "steam": "Steam"}.get(info.get("edition"), "?")
    if info.get("profile"):
        return True, (f"installed: {installed}; running {edition} "
                      f"({pe_text} = {info['profile']}, supported)")
    return False, (f"installed: {installed}; running {edition} by {info.get('edition_from')} "
                   f"({pe_text} is NOT a supported version) {info.get('path') or ''}")


def _web_server():
    from . import server

    async def probe():
        srv = server.HorizonServer(None, ("127.0.0.1", config.MEDIAMTX_WEBRTC_PORT))
        runner = await srv.run(host="127.0.0.1", port=18787)
        try:
            import aiohttp
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            scheme = "https" if srv._tls_on else "http"
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as s:
                base = f"{scheme}://127.0.0.1:18787"
                async with s.get(f"{base}/health") as r:
                    locked = r.status == 401
                async with s.get(f"{base}/pair", params={"code": srv.pair_code}) as r:
                    token = (await r.json()).get("token", "")
                async with s.get(f"{base}/health", headers={"X-HL-Token": token}) as r:
                    body = await r.json()
                if not locked:
                    return False, "the web server answered an unpaired device" 
            return body.get("ok") is True and srv._tls_on, \
                f"{scheme.upper()} ok" if srv._tls_on else "HTTPS certificate failed (phones need HTTPS)"
        finally:
            await runner.cleanup()
    return asyncio.run(probe())


def _horizonlens_running() -> bool:
    import os
    out = subprocess.run(["tasklist", "/fi", "IMAGENAME eq HorizonLens.exe", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, timeout=10,
                         creationflags=0x08000000).stdout
    pids = [int(line.split(",")[1].strip('"')) for line in out.splitlines()
            if line.lower().startswith('"horizonlens.exe"')]
    return any(pid != os.getpid() for pid in pids)


def _media_server():
    if _horizonlens_running():
        busy = streaming.busy_ports()
        if busy and all("mediamtx.exe" in b for b in busy):
            return True, "in use by the running HorizonLens (that's fine)"
        if busy:
            return False, "; ".join(busy)
    sc = streaming.StreamController()
    try:
        if not sc.start_mediamtx():
            return False, sc.last_error or "MediaMTX did not start"
        time.sleep(2.0)
        if sc.mediamtx_alive():
            return True, f"running (ports {config.MEDIAMTX_WEBRTC_PORT}, {config.MEDIAMTX_WEBRTC_UDP}/udp)"
        return False, "exited: " + (streaming.mediamtx_log_tail() or "no output")
    finally:
        sc.stop()


def _tail(path, keep=None, count=8):
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return []
    if keep:
        text = [line for line in text if any(k in line for k in keep)]
    return text[-count:]


def _runtime_logs() -> list[str]:
    import tempfile
    from pathlib import Path
    out = []
    sessions = sorted((config.data_dir() / "session").glob("*/RealCamForza6.log"),
                      key=lambda f: f.stat().st_mtime)
    if sessions:
        log = sessions[-1]
        zoom = _tail(log, keep=("FOV",), count=8)
        capture = _tail(log, keep=("DIRECT_CAPTURE", "REJECT", "UNAVAILABLE",
                                   "INSTANCE_HOOK_INSTALLED"), count=8)
        out += ["", f"Camera module zoom log (game {log.parent.name}):"] \
            + (zoom or ["(no FOV lines -- zoom was never applied or reported)"])
        out += ["", "Camera module capture log:"] + (capture or ["(none)"])
    for name in ("capture", "helper", "relay"):
        path = Path(tempfile.gettempdir()) / f"horizonlens_{name}.log"
        lines = _tail(path, count=40 if name == "helper" else 8)
        if name == "helper":
            starts = [i for i, l in enumerate(lines) if "helper started" in l]
            lines = lines[starts[-1]:] if starts else lines[-8:]
            lines = lines[:12]
        elif name == "capture" and lines and not lines[0].startswith("---"):
            head = _tail(path, count=10_000)[:1]
            lines = head + lines[-7:]
        if lines:
            when = time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime))
            out += ["", f"Live view {name} log (last written {when}):"] + lines
    crash = config.data_dir() / "crash.log"
    if crash.is_file():
        text = crash.read_text(encoding="utf-8", errors="replace").rstrip().split("\n=== ")
        last = text[-1].splitlines()
        if len(last) > 1:
            out += ["", "Last crash / error log:", "=== " + last[0]] + last[1:][-25:]
    phone = _tail(config.data_dir() / "phone.log", count=14)
    out += ["", "Phone player log:"] + (phone or ["(no phone has connected to the live view)"])
    srv_log = _tail(config.data_dir() / "server.log", count=8)
    if srv_log:
        out += ["", "Web server log:"] + srv_log
    return out


def _audio():
    from .recording import _LoopbackAudio
    a = _LoopbackAudio()
    try:
        if not a.available:
            return False, "no loopback device (recordings will be silent)"
        return True, f"{a._device['name']} ({a.rate} Hz)"
    finally:
        a.close()


def _firewall():
    exe = str(config.app_dir() / "HorizonLens.exe")
    out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all",
                          "dir=in", "verbose"], capture_output=True, text=True,
                         timeout=20, creationflags=0x08000000).stdout.lower()
    if exe.lower() in out:
        return True, "inbound rule present"
    return False, "no rule yet: run Allow-Firewall.bat if the phone can't connect"


def run() -> int:
    results = []
    _check(results, "Game", _game)
    _check(results, "ffmpeg", lambda: (config.find_ffmpeg() is not None,
                                        config.find_ffmpeg() or "missing"))
    _check(results, "Video encoder", lambda: (
        True, streaming.h264_encoder(config.find_ffmpeg())))
    _check(results, "Game capture", lambda: (
        streaming.direct_helper() is not None,
        "game-direct helper present" if streaming.direct_helper() else "helper missing (window capture only)"))
    _check(results, "Media server", _media_server)
    _check(results, "Web server", _web_server)
    _check(results, "PC audio", _audio)
    _check(results, "Network", lambda: (
        config.primary_lan_ip() != "127.0.0.1", ", ".join(config.lan_ipv4s()) or "no LAN address"))
    _check(results, "Firewall", _firewall)
    _check(results, "Camera files", lambda: (
        (config.VENDOR / "companion" / "HorizonLensCamera.dll").is_file()
        and (config.VENDOR / "companion" / "xbox" / "HorizonLensCamera.dll").is_file(),
        "Steam + Microsoft Store"))

    lines = [f"HorizonLens {__version__} self-test  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Windows {platform.version()}  Python {platform.python_version()}", ""]
    for ok, name, detail in results:
        lines.append(f"[{'OK ' if ok else 'BAD'}] {name:<14} {detail}")
    attach_log = config.data_dir() / "attach.log"
    if attach_log.is_file():
        entries = attach_log.read_text(encoding="utf-8", errors="replace").split("--- ")[1:]
        start = max([i for i, e in enumerate(entries) if "injecting" in e or "scan" in e.lower()]
                    or [len(entries) - 1])
        lines += ["", "Last camera attach:"] + ["--- " + e.rstrip() for e in entries[start:][-6:]]
    lines += _runtime_logs()
    try:
        from . import vista
        st = vista.status()
        ready = sum(a["ok"] for a in st["actions"])
        lines += ["", f"Forzavista: {'running' if st['running'] else 'not running'}, "
                      f"{ready} of {len(st['actions'])} remote buttons have key bindings"
                      f"{'' if st['file'] else ' (no bindings file yet)'}"]
    except Exception as error:
        lines += ["", f"Forzavista: check failed ({error})"]
    report = _write_report("\n".join(lines) + "\n")
    import os
    if not os.environ.get("HORIZONLENS_NO_OPEN"):
        try:
            if report:
                os.startfile(str(report))
        except Exception:
            pass
    return 0 if all(ok for ok, _, _ in results) else 1


def _write_report(text: str):
    from pathlib import Path
    for folder in (config.app_dir(), Path.home() / "Desktop", config.data_dir()):
        path = folder / "HorizonLens-selftest.txt"
        try:
            path.write_text(text, encoding="utf-8")
            return path
        except OSError:
            continue
    return None


def main():
    try:
        sys.exit(run())
    except Exception:
        _write_report(traceback.format_exc())
        sys.exit(2)
