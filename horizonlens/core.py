from __future__ import annotations

import asyncio
import sys
import threading
import time

from . import (airride, carpose, config, rcf6, sequencer, seqanim, streaming, server, recording,
               game, companion)


CAMERA_TARGET_INDICES = {"driver": 2, "follow_low": 0, "copter": 20}
CAMERA_COMMAND_MODES = {"driver": 0, "copter": 1, "follow_low": 2}


def _game_hwnd(pid: int):
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lp):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(cb, 0)
        return found[0] if found else None
    except Exception:
        return None


def _press_tab(pid: int):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = _game_hwnd(pid)
    if hwnd and user32.GetForegroundWindow() != hwnd:
        try:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
        except Exception:
            pass

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT),
                    ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    for flags in (0, 0x0002):
        inp = INPUT(type=1)
        inp.ki = KEYBDINPUT(0x09, 0x0F, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.05)


class CameraSwitcher:

    def __init__(self, orch: "Orchestrator"):
        self._orch = orch
        self.desired: str | None = None
        self.observed: str | None = None
        self.observed_kind: str | None = None
        self.native_supported: bool | None = None
        self.we_selected = False
        self.exhausted_observation = None
        self.observed_index: int | None = None
        self.attempts = 0
        self.state = "idle"
        self._stable_since: float | None = None
        self._next_press = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def set_target(self, label: str | None):
        with self._lock:
            if label != self.desired:
                self.desired = label
                self.attempts = 0
                self._stable_since = None
                self._next_press = 0.0
                self.state = "switching" if label else "idle"

    def start(self):
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return
            self._thread.join(timeout=1.0)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="hl-camera-switcher")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"desired": self.desired, "observed": self.observed,
                    "observedKind": self.observed_kind,
                    "observedIndex": self.observed_index,
                    "attempts": self.attempts, "state": self.state}

    def _matches(self) -> bool:
        if self.desired is None:
            return False
        if self.observed == self.desired or self.observed_kind == self.desired:
            return True
        return self.observed_index == CAMERA_TARGET_INDICES.get(self.desired)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(0.3)

    def _tick(self):
        orch = self._orch
        srv = orch.server
        link = orch.link
        seq = getattr(srv, "sequencer", None) if srv is not None else None
        seq_engaged = bool(seq is not None and seq.is_engaged())
        if srv is None or (len(getattr(srv, "_clients", ())) == 0 and not seq_engaged):
            with self._lock:
                if self.desired is not None:
                    self.desired = None
                    self.state = "idle"
                we_selected = self.we_selected
                self.we_selected = False
            if we_selected and link is not None:
                try:
                    link.restore_camera()
                except Exception:
                    pass
            return
        if link is None:
            return
        st = srv.fresh_status() if hasattr(srv, "fresh_status") else None
        if st is None and srv.camera_target is not None:
            st = link.status()
        target = srv.camera_target
        if target is not None and st is not None and st.get("exterior_calibrated") is False:
            target = "driver"
        self.set_target(target)
        if self.desired is None or st is None:
            return
        now = time.time()
        with self._lock:
            self.observed = st.get("active_camera") or None
            self.observed_kind = st.get("active_camera_kind") or None
            ns = st.get("native_camera_selection_supported")
            if isinstance(ns, bool):
                self.native_supported = ns
            idx = st.get("active_camera_index")
            self.observed_index = idx if isinstance(idx, int) else None
            if self._matches():
                if self._stable_since is None:
                    self._stable_since = now
                elif now - self._stable_since >= 0.85:
                    self.state = "matched"
                    self.attempts = 0
                    self.exhausted_observation = None
                return
            self._stable_since = None
            if self.state == "matched":
                self.state = "switching"
                self.attempts = 0
                self._next_press = 0.0
            if self.attempts >= 12:
                obs_key = (self.observed, self.observed_index)
                if self.exhausted_observation is None:
                    self.exhausted_observation = obs_key
                    self.state = "exhausted"
                    return
                if self.exhausted_observation != obs_key:
                    self.exhausted_observation = None
                    self.attempts = 0
                    self.state = "switching"
                else:
                    self.state = "exhausted"
                    return
            if self.observed_index is None and self.observed in (None, "unknown"):
                return
            if now < self._next_press:
                return
            self.attempts += 1
            self._next_press = now + 0.9
            self.state = "switching"
            use_native = self.native_supported is not False
            cmd = CAMERA_COMMAND_MODES.get(self.desired)
        if use_native and cmd is not None:
            try:
                link.select_camera(cmd)
                self.we_selected = True
                return
            except Exception:
                pass
        pid = game.game_pid()
        if pid:
            self.we_selected = True
            _press_tab(pid)


def _server_start_problem(error) -> str:
    if isinstance(error, OSError) and getattr(error, "winerror", None) in (10048, 10013):
        return (f"Port {config.WEB_PORT} is in use by another program (or another "
                "HorizonLens). Close it and press Start again.")
    return f"The phone page couldn't start ({error}). Press Stop, then Start."


class Orchestrator:
    def _set_quality(self, name):
        self.recorder.set_quality(name)
        self.stream.set_quality(name)

    def __init__(self):
        self.link: rcf6.CompanionLink | None = None
        self.companion_pid: int | None = None
        self.recorder = recording.Recorder()
        self.stream = streaming.StreamController(
            direct_ready=lambda: self.link is not None,
            hold_restarts=lambda: self.recorder.is_recording() and not self.recorder.dedicated)
        self.recorder.source = self.stream.recording_source
        self.recorder.on_dedicated_failed = lambda: setattr(self.stream, "dedicated_failed", True)
        self.air = airride.AirRide()
        self.sequencer = sequencer.SequencerEngine(
            on_change=sequencer.save_autosave)
        saved = sequencer.load_autosave()
        if saved is not None:
            self.sequencer.seq = saved
        self.car = carpose.CarTracker()
        self.sequencer.car_pose = lambda: self.car.pose(max_age=0.03)
        self.anim = seqanim.AnimationDriver(self.sequencer, air=self.air)
        self.server: server.HorizonServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner = None
        self.lan_ip = config.primary_lan_ip()
        self.running = False
        self.switcher = CameraSwitcher(self)
        self._attach_lock = threading.Lock()
        self._life_lock = threading.RLock()
        self._record_lock = threading.Lock()

    def _session_roots(self):
        roots = [str(companion.session_dir())]
        roots += rcf6.default_sessions_roots()
        return roots

    def refresh_companion(self) -> bool:
        if not self._attach_lock.acquire(blocking=False):
            return self.link is not None
        try:
            return self._refresh_companion()
        finally:
            self._attach_lock.release()

    def _refresh_companion(self) -> bool:
        gpid = game.game_pid()
        if self.link is not None and gpid is not None \
                and self.companion_pid != gpid:
            try:
                self.link.close()
            except Exception:
                pass
            self.link = None
            self.companion_pid = None
            if self.server:
                self.server.link = None
        if gpid is None:
            if self.link is not None:
                try:
                    self.link.close()
                except Exception:
                    pass
                self.link = None
                self.companion_pid = None
                if self.server:
                    self.server.link = None
            return False
        for root in self._session_roots():
            found = rcf6.discover_session_key(root)
            if found:
                key, pid, image_base, path = found
                if gpid is not None and pid != gpid:
                    continue
                if self.companion_pid != pid or self.link is None:
                    if self.link:
                        self.link.close()
                    self.link = rcf6.CompanionLink(key)
                    self.companion_pid = pid
                    if self.server:
                        self.server.link = self.link
                return True
        return False

    def _use_key(self, key: bytes, pid: int):
        if self.link:
            self.link.close()
        self.link = rcf6.CompanionLink(key)
        self.companion_pid = pid
        if self.server:
            self.server.link = self.link

    def attach_companion(self, progress=lambda m: None) -> tuple[bool, str]:
        with self._attach_lock:
            if self.link is not None and self.companion_pid == game.game_pid():
                return True, "Companion already linked."
            if self._refresh_companion():
                return True, "Camera control: ready (reconnected to the camera module)."
            res = companion.attach(progress=progress)
            if res.ok and res.key:
                self._use_key(res.key, res.pid)
            return res.ok, res.message

    def game_status(self) -> dict:
        return game.status()

    def launch_game(self, edition: str | None = None) -> bool:
        return game.launch(edition)

    def start(self, start_capture: bool = True) -> dict:
        with self._life_lock:
            if self.running:
                return self.status()
            return self._start(start_capture)

    def _start(self, start_capture):
        self.lan_ip = config.primary_lan_ip()
        self.server_error = None
        self.refresh_companion()
        self.server = None
        if start_capture:
            self.stream.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.switcher.start()
        self.air.start()
        self.anim.start()
        for _ in range(60):
            if self.server_error or (self.server is not None and self._runner is not None):
                break
            time.sleep(0.05)
        self.running = True
        return self.status()

    def _run_loop(self):
        self._loop = (asyncio.SelectorEventLoop() if sys.platform == "win32"
                      else asyncio.new_event_loop())
        asyncio.set_event_loop(self._loop)

        def log_loop_error(loop, context):
            error = context.get("exception")
            server.server_log(f"event loop: {context.get('message')} {error!r}")
            loop.default_exception_handler(context)
        self._loop.set_exception_handler(log_loop_error)
        self.server = server.HorizonServer(
            self.link, (self.lan_ip, config.MEDIAMTX_WEBRTC_PORT)
        )
        self.server.record_toggle = self.toggle_recording
        self.server.viewport_hook = self.stream.set_aspect
        self.server.wide_hook = self.stream.set_wide
        self.server.keyframe_hook = self.stream.request_keyframe
        self.server.livefail_hook = self.stream.phone_cannot_decode
        self.server.baseline_hook = self.stream.set_baseline
        self.server.quality_hook = self._set_quality
        self.server.stream_generation = lambda: getattr(self.stream, "generation", 0)
        self.server.switcher = self.switcher
        self.server.air = self.air
        self.server.speed_source = lambda: self.car.speed(max_age=0.05)
        self.server.pose_source = lambda: self.car.pose(max_age=0.02)
        self.server.cars_source = lambda: self.car.others(max_age=0.0, traffic=False)
        self.server.sequencer = self.sequencer
        self.server.recording_state = self.recorder.is_recording
        try:
            self._runner = self._loop.run_until_complete(
                self.server.run(host="0.0.0.0", port=config.WEB_PORT))
        except Exception as error:
            self.server_error = _server_start_problem(error)
            server.server_log(f"web server could not start: {error!r}")
            self.server = None
            self._loop.close()
            return
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def toggle_recording(self) -> bool:
        with self._record_lock:
            return self._toggle_recording()

    def _toggle_recording(self) -> bool:
        if self.recorder.is_recording():
            self.recorder.stop()
            return False
        if not self.stream.capture_alive():
            self.recorder.stop()
            self.recorder.last_error = "no live view to record yet"
            return False
        self.recorder.start()
        return self.recorder.is_recording()

    def stop(self):
        with self._life_lock:
            self._stop()

    def _stop(self):
        self.running = False
        self.sequencer.stop()
        self.anim.stop()
        self.switcher.stop()
        self.air.stop()
        if self.recorder.is_recording():
            self.recorder.stop()
        self.stream.stop()
        if self._loop and self._loop.is_running():
            async def _shutdown():
                try:
                    if self._runner:
                        await self._runner.cleanup()
                except Exception:
                    pass
                self._loop.stop()
            self._loop.call_soon_threadsafe(
                lambda: self._loop.create_task(_shutdown()))
        if self._thread:
            self._thread.join(timeout=3)
        self.server = None
        self._runner = None
        if self.link:
            try:
                self.link.send_pose(rcf6.Pose(kind=7))
            except Exception:
                pass
            self.link.close()
            self.link = None

    def client_url(self) -> str:
        return f"https://{self.lan_ip}:{config.WEB_PORT}/"

    def pair_code(self) -> str:
        return self.server.pair_code if self.server else "--------"

    def status(self) -> dict:
        return {
            "lan_ip": self.lan_ip,
            "url": self.client_url(),
            "pair_code": self.pair_code(),
            "companion": self.link is not None,
            "companion_pid": self.companion_pid,
            "mediamtx": self.stream.mediamtx_alive(),
            "capture": self.stream.capture_alive(),
            "capture_mode": self.stream.describe(),
            "capture_error": self.stream.capture_error(),
            "clients": len(self.server._clients) if self.server else 0,
            "server_error": getattr(self, "server_error", None),
        }
