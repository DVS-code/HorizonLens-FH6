from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, procjob

RECORD_SIZES = {"high": (1920, 24_000_000), "max": (None, 50_000_000)}
RECORD_MAX_EDGE = 3840
READY_SECONDS = 6.0


def record_size(crop_w: int, crop_h: int, quality: str) -> tuple[int, int]:
    long_edge = max(crop_w, crop_h, 2)
    want, _ = RECORD_SIZES[quality]
    if want is None:
        want = max(1920, min(RECORD_MAX_EDGE, long_edge))
    scale = want / long_edge
    return max(2, round(crop_w * scale / 2) * 2), max(2, round(crop_h * scale / 2) * 2)

_NOWIN = 0x08000000 if sys.platform == "win32" else 0


class _LoopbackAudio:

    def __init__(self):
        self.rate = 48000
        self.channels = 2
        self._pa = None
        self._device = None
        try:
            import pyaudiowpatch as pyaudio
            self._pa = pyaudio.PyAudio()
            self._device = self._pa.get_default_wasapi_loopback()
            self.rate = int(self._device["defaultSampleRate"])
            self.channels = max(1, min(2, int(self._device["maxInputChannels"])))
        except Exception:
            self.close()

    @property
    def available(self) -> bool:
        return self._device is not None

    def pump(self, port: int, stop: threading.Event):
        import socket
        import pyaudiowpatch as pyaudio
        sink = None
        while sink is None and not stop.is_set():
            try:
                sink = socket.create_connection(("127.0.0.1", port), timeout=1)
            except OSError:
                stop.wait(0.1)
        if sink is None:
            return
        frames = self.rate // 50
        silence = b"\x00" * frames * self.channels * 2
        try:
            stream = self._pa.open(format=pyaudio.paInt16, channels=self.channels,
                                   rate=self.rate, input=True, frames_per_buffer=frames,
                                   input_device_index=self._device["index"])
        except Exception:
            sink.close()
            return
        try:
            while not stop.is_set():
                if stream.get_read_available() >= frames:
                    data = stream.read(frames, exception_on_overflow=False)
                else:
                    stop.wait(0.02)
                    if stream.get_read_available() >= frames:
                        continue
                    data = silence
                sink.sendall(data)
        except Exception:
            pass
        finally:
            for done in (stream.stop_stream, stream.close, sink.close):
                try:
                    done()
                except Exception:
                    pass

    def close(self):
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._pa = None
        self._device = None


class Recorder:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._audio: _LoopbackAudio | None = None
        self._audio_thread: threading.Thread | None = None
        self._stop_audio = threading.Event()
        self.current_path: Path | None = None
        self.last_error: str | None = None
        self.quality = "standard"
        self.source = None
        self.on_dedicated_failed = None
        self._helper: subprocess.Popen | None = None
        self.dedicated = False
        self._lock = threading.RLock()

    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        with self._lock:
            return self._start()

    def stop(self) -> Path | None:
        with self._lock:
            return self._stop()

    def set_quality(self, name: str):
        if name in ("standard", "high", "max"):
            self.quality = name

    def _open_dedicated(self):
        from .streaming import centre_crop
        src = self.source() if self.source else None
        if not src:
            return None
        cw, ch = src["client"]
        left, top, right, bottom = centre_crop(cw, ch, src["aspect"])
        ow, oh = record_size(cw - left - right, ch - top - bottom, self.quality)
        fps = int(src["fps"])
        aw, ah = src["aspect"]
        cmd = [src["helper"], str(cw), str(ch), "0", "0", str(fps),
               str(RECORD_SIZES[self.quality][1]), "--secondary",
               "--fit-aspect", f"{aw}:{ah}", "--output-size", f"{ow}x{oh}"]
        if src.get("pid"):
            cmd += ["--source-pid", str(int(src["pid"]))]
        ready, failed = threading.Event(), threading.Event()
        log = open(config.recordings_dir() / ".last-recording-capture.log", "w", encoding="utf-8")
        log.write(" ".join(cmd) + "\n")
        try:
            helper = procjob.own(subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=_NOWIN))
        except OSError as error:
            log.write(f"start failed: {error}\n")
            log.close()
            return None

        def read():
            for raw in helper.stderr:
                line = raw.decode(errors="replace").strip()
                log.write(line + "\n")
                log.flush()
                if "REALCAM_FORZA_NVENC_READY" in line:
                    ready.set()
                elif "REALCAM_FORZA_NVENC_ERROR" in line or "NOT_NVIDIA" in line:
                    failed.set()
            failed.set()
            log.write(f"--- exited code={helper.poll()}\n")
            log.close()
        threading.Thread(target=read, daemon=True, name="hl-record-capture").start()
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline and not ready.is_set() and not failed.is_set():
            ready.wait(0.05)
        if not ready.is_set():
            try:
                helper.stdin.close()
            except Exception:
                pass
            try:
                helper.kill()
            except Exception:
                pass
            return None
        return helper, fps

    def _start(self) -> bool:
        if self.is_recording():
            return True
        self.stop()
        ffmpeg = config.find_ffmpeg()
        if not ffmpeg:
            self.last_error = "ffmpeg not found"
            return False
        ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = config.recordings_dir() / f"HorizonLens_{ts}.mp4"
        rtsp = f"rtsp://127.0.0.1:{config.MEDIAMTX_RTSP_PORT}/{config.STREAM_PATH}"
        dedicated = self._open_dedicated() if self.quality in RECORD_SIZES else None
        if self.quality in RECORD_SIZES and dedicated is None and self.on_dedicated_failed:
            self.on_dedicated_failed()
        audio = _LoopbackAudio()
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
               "-use_wallclock_as_timestamps", "1"]
        if dedicated is not None:
            cmd += ["-probesize", "16M", "-analyzeduration", "5M",
                    "-f", "h264", "-framerate", str(dedicated[1]), "-i", "pipe:0"]
        else:
            cmd += ["-rtsp_transport", "tcp", "-i", rtsp]
        audio_port = 0
        if audio.available:
            import socket
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                audio_port = probe.getsockname()[1]
            cmd += ["-use_wallclock_as_timestamps", "1",
                    "-f", "s16le", "-ar", str(audio.rate),
                    "-ac", str(audio.channels),
                    "-i", f"tcp://127.0.0.1:{audio_port}?listen=1",
                    "-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0"]
        import time
        cmd += ["-copyts", "-output_ts_offset", f"-{time.time():.6f}",
                "-c:v", "copy"]
        if audio.available:
            cmd += ["-af", "aresample=async=1000", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(out)]
        self._log = open(config.recordings_dir() / ".last-recording.log", "wb")
        self._proc = subprocess.Popen(
            cmd, stdin=dedicated[0].stdout if dedicated else subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=self._log, creationflags=_NOWIN)
        procjob.own(self._proc)
        self._helper = dedicated[0] if dedicated else None
        self.dedicated = dedicated is not None
        if dedicated:
            dedicated[0].stdout.close()
        self.current_path = out
        self.last_error = None if audio.available else "no PC audio device"
        if audio.available:
            self._audio = audio
            self._stop_audio.clear()
            self._audio_thread = threading.Thread(
                target=audio.pump, args=(audio_port, self._stop_audio),
                daemon=True, name="hl-record-audio")
            self._audio_thread.start()
        else:
            audio.close()
        return True

    def _stop(self) -> Path | None:
        proc = self._proc
        if proc is None:
            return None
        helper, self._helper = self._helper, None
        if proc.poll() is None:
            try:
                if helper is not None:
                    try:
                        helper.stdin.close()
                    except Exception:
                        pass
                    self._stop_audio.set()
                else:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.terminate(); proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        else:
            self.last_error = f"recording stopped by itself (ffmpeg exit {proc.returncode})"
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        if helper is not None:
            try:
                helper.wait(timeout=3)
            except Exception:
                helper.kill()
        self.dedicated = False
        if self._audio is not None:
            self._stop_audio.set()
            if self._audio_thread is not None:
                self._audio_thread.join(timeout=2)
            self._audio.close()
            self._audio = None
        self._audio_thread = None
        if getattr(self, "_log", None) is not None:
            self._log.close()
            self._log = None
        path = self.current_path
        self._proc = None
        return path if path is not None and path.is_file() else None


def list_recordings() -> list[dict]:
    out = []
    for p in config.recordings_dir().glob("*.mp4"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"path": str(p), "name": p.name, "size_mb": round(st.st_size / 1e6, 1),
                    "duration": probe_duration(p), "_mtime": st.st_mtime})
    out.sort(key=lambda c: c.pop("_mtime"), reverse=True)
    return out


def probe_duration(path: Path) -> float | None:
    ffprobe = config.find_ffprobe()
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=15, creationflags=_NOWIN)
        data = json.loads(r.stdout or "{}")
        return float(data.get("format", {}).get("duration", 0.0)) or None
    except Exception:
        return None


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600); m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def split_at(path: str, cut_points: list[float]) -> list[str]:
    ffmpeg = config.find_ffmpeg()
    src = Path(path)
    if not ffmpeg or not src.is_file():
        return []
    dur = probe_duration(src) or 0.0
    bounds = [0.0] + sorted(t for t in cut_points if 0 < t < dur) + [dur]
    out_paths = []
    scenes_dir = src.parent / f"{src.stem}_scenes"
    scenes_dir.mkdir(exist_ok=True)
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a < 0.05:
            continue
        out = scenes_dir / f"{src.stem}_scene{i+1:02d}.mp4"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", _fmt_ts(a), "-to", _fmt_ts(b), "-i", str(src),
            "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN, timeout=600)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and out.is_file():
            out_paths.append(str(out))
    return out_paths


def trim(path: str, start: float, end: float) -> str | None:
    ffmpeg = config.find_ffmpeg()
    src = Path(path)
    if not ffmpeg or not src.is_file() or end <= start:
        return None
    out = src.parent / f"{src.stem}_trim_{int(start)}-{int(end)}.mp4"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", _fmt_ts(start), "-to", _fmt_ts(end), "-i", str(src),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    return str(out) if r.returncode == 0 and out.is_file() else None
