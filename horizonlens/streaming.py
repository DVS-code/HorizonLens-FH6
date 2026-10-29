from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config, game, procjob

_NOWIN = 0x08000000 if sys.platform == "win32" else 0

_MTX_CONFIG = """
logLevel: info
logDestinations: [stdout]
readTimeout: 10s
writeTimeout: 10s
writeQueueSize: 512
udpMaxPayloadSize: 1472

api: yes
apiAddress: 127.0.0.1:{api}

rtsp: yes
rtspTransports: [tcp]
rtspAddress: 127.0.0.1:{rtsp}

rtmp: no
hls: no
srt: no
moq: no

webrtc: yes
webrtcAddress: :{webrtc}
webrtcEncryption: no
webrtcAllowOrigins: ['*']
webrtcLocalUDPAddress: :{webrtc_udp}
webrtcLocalTCPAddress: ''
webrtcIPsFromInterfaces: yes
webrtcAdditionalHosts: [{additional_hosts}]

paths:
  {path}:
    source: publisher
    overridePublisher: yes
"""


def write_mediamtx_config() -> Path:
    hosts = config.lan_ipv4s()
    additional = ", ".join(f"'{h}'" for h in hosts) if hosts else ""
    cfg = _MTX_CONFIG.format(
        api=config.MEDIAMTX_API_PORT,
        rtsp=config.MEDIAMTX_RTSP_PORT,
        webrtc=config.MEDIAMTX_WEBRTC_PORT,
        webrtc_udp=config.MEDIAMTX_WEBRTC_UDP,
        additional_hosts=additional,
        path=config.STREAM_PATH,
    )
    import os
    work = config.data_dir() / "mediamtx"
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"mediamtx-{os.getpid()}.yml"
    out.write_text(cfg, encoding="utf-8")
    return out


_ENCODER_CACHE: dict[str, str] = {}


def h264_encoder(ffmpeg: str) -> str:
    if ffmpeg in _ENCODER_CACHE:
        return _ENCODER_CACHE[ffmpeg]
    chosen = "libx264"
    for name in ("h264_nvenc", "h264_amf"):
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=black:s=320x240:r=30", "-frames:v", "3",
                 "-pix_fmt", "yuv420p", "-c:v", name, "-f", "null", "-"],
                capture_output=True, timeout=15, creationflags=_NOWIN)
            if r.returncode == 0:
                chosen = name
                break
        except Exception:
            continue
    _ENCODER_CACHE[ffmpeg] = chosen
    return chosen


def _port_owner(port: int, udp: bool) -> str:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "UDP" if udp else "TCP"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_NOWIN).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1].endswith(f":{port}")                     and (udp or parts[3] == "LISTENING"):
                pid = parts[-1]
                name = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                                      capture_output=True, text=True, timeout=10,
                                      creationflags=_NOWIN).stdout.split(",")[0].strip('"')
                return name or f"pid {pid}"
    except Exception:
        pass
    return ""


def busy_ports() -> list[str]:
    import socket
    checks = [(config.MEDIAMTX_RTSP_PORT, False, "127.0.0.1"),
              (config.MEDIAMTX_WEBRTC_PORT, False, "0.0.0.0"),
              (config.MEDIAMTX_API_PORT, False, "127.0.0.1"),
              (config.MEDIAMTX_WEBRTC_UDP, True, "0.0.0.0")]
    busy = []
    for port, udp, host in checks:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((host, port))
        except OSError:
            owner = _port_owner(port, udp)
            busy.append(f"port {port}/{'udp' if udp else 'tcp'} is in use"
                        + (f" by {owner}" if owner else ""))
        finally:
            sock.close()
    return busy


def mediamtx_log_tail(lines: int = 6) -> str:
    try:
        logs = sorted((config.data_dir() / "mediamtx").glob("mediamtx-*.log"),
                      key=lambda f: f.stat().st_mtime)
        if not logs:
            return ""
        text = logs[-1].read_text(encoding="utf-8", errors="replace")
        return " | ".join(l.strip() for l in text.strip().splitlines()[-lines:])
    except OSError:
        return ""


def direct_helper() -> Path | None:
    for candidate in (config.VENDOR / "capture" / "HorizonLensCapture.exe",
                      Path(config.realcam_root()) / "Desktop" / "RealCamForzaNvencCapture.exe"):
        if candidate.is_file():
            return candidate
    return None


def reduced_aspect(width: int, height: int) -> tuple[int, int]:
    width, height = max(1, int(width)), max(1, int(height))
    g = math.gcd(width, height)
    return width // g, height // g


QUALITY = {
    "standard": (12_000_000, "p1"),
    "high": (30_000_000, "p3"),
    "max": (50_000_000, "p4"),
}
CPU_MAX_BPS = 20_000_000

DECODE_LONG_EDGE = 1920
DECODE_SHORT_EDGE = 1088


def decode_budget(width: int, height: int) -> tuple[int, int]:
    long_edge, short_edge = max(width, height, 2), max(min(width, height), 2)
    scale = min(1.0, DECODE_LONG_EDGE / long_edge, DECODE_SHORT_EDGE / short_edge)
    return max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1)


def centre_crop(src_w: int, src_h: int, aspect: tuple[int, int]):
    aw, ah = aspect
    if src_w * ah > src_h * aw:
        w = (src_h * aw // ah) & ~1
        left = ((src_w - w) // 2) & ~1
        return left, 0, src_w - w - left, 0
    h = (src_w * ah // aw) & ~1
    top = ((src_h - h) // 2) & ~1
    return 0, top, 0, src_h - h - top


class StreamController:

    DIRECT = "game_direct"
    WINDOW = "game_window"

    def __init__(self, direct_ready=lambda: False, hold_restarts=lambda: False):
        self.direct_ready = direct_ready
        self.hold_restarts = hold_restarts
        self._mtx: subprocess.Popen | None = None
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._log = None
        self.mode: str | None = None
        self.encoder: str | None = None
        self.orientation: tuple[int, int] = (9, 16)
        self.wide: tuple[int, int] | None = None
        self._running_aspect = None
        self.baseline = False
        self._running_baseline = False
        self.quality = "standard"
        self._running_rate = None
        self.dedicated_failed = False
        self._running_pid = None
        self._running_size = None
        self._new_size = (None, 0)
        self._direct_failed_pid = None
        self._direct_failures = 0
        self._started_at = 0.0
        self._window_rung = 0
        self.window_rung = None
        self._ready = False
        self.last_error: str | None = None

    def start_mediamtx(self) -> bool:
        exe = config.mediamtx_exe()
        if not exe.is_file():
            self.last_error = f"MediaMTX not found at {exe}"
            return False
        busy = busy_ports()
        if busy and any("mediamtx" in b.lower() for b in busy)                 and procjob.clear_orphaned_mediamtx():
            time.sleep(0.5)
            busy = busy_ports()
        if busy:
            self.last_error = "; ".join(busy)
            return False
        work = config.data_dir() / "mediamtx"
        work.mkdir(parents=True, exist_ok=True)
        import os
        for stale in (sorted(work.glob("mediamtx-*.log"), key=lambda f: f.stat().st_mtime)[:-5]
                      + [c for c in work.glob("mediamtx-*.yml")
                         if c.stem != f"mediamtx-{os.getpid()}"
                         and time.time() - c.stat().st_mtime > 86400]):
            try:
                stale.unlink()
            except OSError:
                pass
        self._mtx_log = open(work / f"mediamtx-{os.getpid()}.log", "wb")
        self._mtx_config = write_mediamtx_config()
        self._mtx = procjob.own(subprocess.Popen(
            [str(exe), str(self._mtx_config)], cwd=str(work),
            stdout=self._mtx_log, stderr=subprocess.STDOUT, creationflags=_NOWIN))
        return True

    def start(self, ffmpeg: str | None = None) -> bool:
        self.ffmpeg = ffmpeg or config.find_ffmpeg()
        if not self.ffmpeg:
            self.last_error = "ffmpeg not found"
            return False
        if not self.start_mediamtx():
            return False
        time.sleep(0.8)
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, daemon=True,
                                        name="hl-capture")
        self._thread.start()
        return True

    def set_aspect(self, width: int, height: int):
        aspect = (9, 16) if int(height) > int(width) else (16, 9)
        if aspect != self.orientation:
            self.orientation = aspect

    def set_baseline(self, on: bool):
        self.baseline = bool(on)

    def set_quality(self, name: str):
        if name in QUALITY:
            self.quality = name

    def _live_quality(self) -> str:
        if self.quality != "standard" and (self.dedicated_failed or direct_helper() is None):
            return self.quality
        return "standard"

    @property
    def bitrate_bps(self) -> int:
        return QUALITY[self._live_quality()][0]

    @property
    def bitrate(self) -> str:
        return f"{self.bitrate_bps // 1_000_000}M"

    @property
    def nvenc_preset(self) -> str:
        return QUALITY[self._live_quality()][1]

    def recording_source(self):
        if self.dedicated_failed or direct_helper() is None or not self.direct_ready():
            return None
        pid = game.game_pid()
        window = game.main_window(pid) if pid else None
        if not window:
            return None
        return {"helper": str(direct_helper()), "pid": pid, "client": tuple(window[1:3]),
                "aspect": self.aspect, "fps": config.CAPTURE_FPS}

    def set_wide(self, aspect):
        if aspect is not None:
            w, h = (int(v) for v in aspect)
            if not (0 < w <= 32 and 0 < h <= 32 and 9 * h <= 16 * w):
                return
            aspect = (w, h) if w * 9 <= h * 16 else (16, 9)
        self.wide = aspect

    @property
    def aspect(self) -> tuple[int, int]:
        if self.wide and self.orientation == (9, 16):
            return self.wide
        return self.orientation

    def phone_cannot_decode(self):
        if self.mode == self.DIRECT:
            self._direct_failed_pid = self._running_pid
            self.last_error = "phone could not play the game-direct stream"

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._stop_capture()
        if self._mtx and self._mtx.poll() is None:
            self._mtx.terminate()
            try:
                self._mtx.wait(timeout=3)
            except Exception:
                self._mtx.kill()
        self._mtx = None
        if getattr(self, "_mtx_log", None):
            self._mtx_log.close()
            self._mtx_log = None
        if getattr(self, "_mtx_config", None):
            try:
                self._mtx_config.unlink()
            except OSError:
                pass
            self._mtx_config = None

    def mediamtx_alive(self) -> bool:
        return self._mtx is not None and self._mtx.poll() is None

    def capture_alive(self) -> bool:
        with self._lock:
            return bool(self._procs) and all(p.poll() is None for p in self._procs)

    def capture_error(self) -> str | None:
        return self.last_error

    def describe(self) -> str:
        if not self.capture_alive():
            return "waiting for the game window"
        source = "game direct" if self.mode == self.DIRECT else "game window"
        if self.mode == self.WINDOW and self.window_rung == "nv12":
            source += " (NV12)"
        enc = {"h264_nvenc": "NVENC", "h264_amf": "AMD", "libx264": "CPU"}.get(
            self.encoder, self.encoder or "")
        return f"{source} · {enc}"

    def whep_path(self) -> str:
        return f"/{config.STREAM_PATH}/whep"

    def _supervise(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as error:
                self.last_error = f"capture supervisor: {error!r}"
            self._stop.wait(1.0)

    MTX_RESTART_SECONDS = 5.0

    def _keep_mediamtx(self) -> bool:
        if self._mtx is None or self._mtx.poll() is None:
            return True
        now = time.monotonic()
        if now - getattr(self, "_mtx_restarted", 0.0) < self.MTX_RESTART_SECONDS:
            return False
        self._mtx_restarted = now
        self.last_error = f"media server stopped (exit {self._mtx.returncode}); restarting it"
        self._stop_capture()
        if getattr(self, "_mtx_log", None):
            self._mtx_log.close()
            self._mtx_log = None
        if not self.start_mediamtx():
            return False
        self._running_size = None
        return False

    def _tick(self):
        if not self._keep_mediamtx():
            return
        pid = game.game_pid()
        window = game.main_window(pid) if pid else None
        alive = self.capture_alive()
        if not pid or not window:
            if self._procs:
                self._stop_capture()
            return
        want_direct = (self.direct_ready() and direct_helper() is not None
                       and not self.baseline
                       and self._direct_failed_pid != pid)
        want = self.DIRECT if want_direct else self.WINDOW
        size = tuple(window[1:3])
        size_ok = True
        if alive and self._running_size is not None and size != self._running_size:
            seen, count = self._new_size
            self._new_size = (size, count + 1 if seen == size else 1)
            size_ok = self._new_size[1] < 2
        else:
            self._new_size = (None, 0)
        if alive and self.mode == want and self._running_aspect == self.aspect \
                and self._running_pid == pid and self._running_baseline == self.baseline \
                and self._running_rate == (self.bitrate_bps, self.nvenc_preset) and size_ok:
            return
        if alive and self._running_pid == pid and self.hold_restarts():
            return
        if self._procs and self.mode == self.DIRECT and not alive:
            if not self._ready or time.time() - self._started_at < 20:
                self._direct_failures += 1
            if not self._ready or self._direct_failures >= 2:
                self._direct_failed_pid = pid
                want = self.WINDOW
        if self._procs and self.mode == self.WINDOW and not alive                 and time.time() - self._started_at < 20:
            self._window_rung += 1
        self._stop_capture()
        if pid != self._running_pid:
            self._direct_failures = 0
            self.dedicated_failed = False
            self._window_rung = 0
        self._started_at = time.time()
        self.generation = getattr(self, "generation", 0) + 1
        if want == self.DIRECT:
            self._start_direct(window)
        else:
            self._start_window(window)
        self._running_pid = pid
        self._running_size = size
        self._new_size = (None, 0)
        self._running_aspect = self.aspect
        self._running_baseline = self.baseline
        self._running_rate = (self.bitrate_bps, self.nvenc_preset)

    def _rtsp_url(self):
        return f"rtsp://127.0.0.1:{config.MEDIAMTX_RTSP_PORT}/{config.STREAM_PATH}"

    def _open_log(self, name):
        self._log = open(Path(tempfile.gettempdir()) / f"horizonlens_{name}.log", "wb")
        return self._log

    def _start_direct(self, window):
        _, client_w, client_h = window
        aw, ah = self.aspect
        helper = [str(direct_helper()), str(client_w), str(client_h), "0", "0",
                  str(config.CAPTURE_FPS), str(self.bitrate_bps),
                  "--fit-aspect", f"{aw}:{ah}",
                  "--output-budget", f"{DECODE_LONG_EDGE}x{DECODE_SHORT_EDGE}"]
        self._ready = False
        producer = procjob.own(subprocess.Popen(helper, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, creationflags=_NOWIN))
        self._helper_stdin = producer.stdin
        relay = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats",
             "-probesize", "16M", "-analyzeduration", "5M",
             "-use_wallclock_as_timestamps", "1",
             "-f", "h264", "-framerate", str(config.CAPTURE_FPS),
             "-i", "pipe:0", "-map", "0:v:0", "-c:v", "copy", "-an",
             "-f", "rtsp", "-rtsp_transport", "tcp", self._rtsp_url()],
            stdin=producer.stdout, stdout=subprocess.DEVNULL,
            stderr=self._open_log("relay"), creationflags=_NOWIN)
        procjob.own(relay)
        producer.stdout.close()
        with self._lock:
            self._procs = [producer, relay]
            self.mode, self.encoder = self.DIRECT, "h264_nvenc"
        threading.Thread(target=self._read_helper_log, args=(producer,),
                         daemon=True, name="hl-capture-log").start()
        threading.Thread(target=self._keyframe_pump, args=(producer,),
                         daemon=True, name="hl-keyframes").start()

    def _read_helper_log(self, producer):
        log = open(Path(tempfile.gettempdir()) / "horizonlens_helper.log", "a",
                   encoding="utf-8")
        log.write(f"--- {time.strftime('%H:%M:%S')} helper started aspect={self.aspect}\n")
        for raw in producer.stderr:
            line = raw.decode(errors="replace").strip()
            log.write(line + "\n")
            log.flush()
            if "REALCAM_FORZA_NVENC_READY" in line:
                self._ready = True
                self.last_error = None
                if "encoder=amf" in line.lower():
                    self.encoder = "h264_amf"
            elif "REALCAM_FORZA_NVENC_ERROR" in line or "NOT_NVIDIA" in line:
                self.last_error = line.split(" ", 1)[-1]
        log.write(f"--- helper exited code={producer.poll()}\n")
        log.close()

    WINDOW_RUNGS = ("gpu", "nv12", "cpu")

    def _start_window(self, window):
        hwnd, client_w, client_h = window
        left, top, right, bottom = centre_crop(client_w, client_h, self.aspect)
        crop_w, crop_h = client_w - left - right, client_h - top - bottom
        out_w, out_h = decode_budget(crop_w, crop_h)
        scale = (f":width={out_w}:height={out_h}:resize_mode=scale:scale_mode=bilinear"
                 if (out_w, out_h) != (crop_w, crop_h) else "")
        source = (f"gfxcapture=hwnd={hwnd}:capture_cursor=0:capture_border=0"
                  f":display_border=0:max_framerate={config.CAPTURE_FPS}"
                  f":crop_left={left}:crop_top={top}:crop_right={right}"
                  f":crop_bottom={bottom}{scale}:output_fmt=8bit")
        encoder = h264_encoder(self.ffmpeg)
        rung = self.WINDOW_RUNGS[min(self._window_rung, 2)]
        if encoder == "libx264":
            rung = "cpu"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning",
               "-init_hw_device", "d3d11va=hl", "-filter_hw_device", "hl",
               "-f", "lavfi", "-i", source]
        gop = str(config.CAPTURE_FPS)
        if rung == "nv12":
            cmd += ["-vf", "hwdownload,format=bgra,format=nv12"]
        if rung in ("gpu", "nv12") and encoder == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", self.nvenc_preset, "-tune", "ll",
                    "-profile:v", "baseline" if self.baseline else "main",
                    "-rc", "cbr", "-b:v", self.bitrate,
                    "-g", gop, "-bf", "0"]
        elif rung in ("gpu", "nv12") and encoder == "h264_amf":
            cmd += ["-c:v", "h264_amf", "-usage", "ultralowlatency",
                    "-profile:v", "constrained_baseline" if self.baseline else "main",
                    "-rc", "cbr", "-b:v", self.bitrate, "-g", gop, "-bf", "0",
                    "-force_key_frames", "expr:gte(t,n_forced*1)", "-forced_idr", "1",
                    "-header_spacing", gop, "-bsf:v", "dump_extra=freq=keyframe"]
        else:
            encoder = "libx264"
            cmd += ["-vf", "hwdownload,format=bgra",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                    "-b:v", f"{min(self.bitrate_bps, CPU_MAX_BPS) // 1_000_000}M", "-g", gop]
        cmd += ["-an", "-f", "rtsp", "-rtsp_transport", "tcp", self._rtsp_url()]
        log = self._open_log("capture")
        log.write(f"--- {time.strftime('%H:%M:%S')} window capture rung={rung} encoder={encoder} "
                  f"baseline={self.baseline} out={out_w}x{out_h}\n".encode())
        log.flush()
        proc = procjob.own(subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=log, creationflags=_NOWIN))
        with self._lock:
            self._procs = [proc]
            self.mode, self.encoder = self.WINDOW, encoder
            self.window_rung = rung
        self._ready = True

    def request_keyframe(self):
        pipe = getattr(self, "_helper_stdin", None)
        if pipe is None or self.mode != self.DIRECT:
            return
        try:
            pipe.write(b"k")
            pipe.flush()
        except (OSError, ValueError):
            pass

    def _keyframe_pump(self, producer):
        while producer.poll() is None and not self._stop.is_set():
            self._stop.wait(2.0)
            if self._helper_stdin is producer.stdin:
                self.request_keyframe()

    def _stop_capture(self):
        with self._lock:
            procs, self._procs = self._procs, []
        pipe, self._helper_stdin = getattr(self, "_helper_stdin", None), None
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
        procs = list(reversed(procs))
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
        if self._log:
            self._log.close()
            self._log = None
