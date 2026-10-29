from __future__ import annotations

import asyncio
import collections
import json
import math
import secrets
import threading
import time

from aiohttp import web, WSMsgType

from . import config, playertrack, rcf6, relay, tls


def _game_label() -> str:
    try:
        from . import game
        edition = game.running_edition()
    except Exception:
        edition = None
    return {"steam": "Forza Horizon 6 (Steam)", "xbox": "Forza Horizon 6 (Microsoft Store)"}.get(
        edition, "Forza Horizon 6")


def server_log(line: str):
    try:
        path = config.data_dir() / "server.log"
        if path.is_file() and path.stat().st_size > 200_000:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


class HorizonServer:
    SEND_HZ = 90.0
    STALE_HOLD_SECONDS = 0.9

    def __init__(self, link: rcf6.CompanionLink | None, stream_whep_host_port):
        self.link = link
        self.stream_whep_host_port = stream_whep_host_port
        self.pair_code = self._make_code()
        self._tokens: dict[str, float] = {}
        self.app = web.Application(middlewares=[self._require_pairing])
        self._stopped = threading.Event()

        async def _on_cleanup(app):
            self._stopped.set()
        self.app.on_cleanup.append(_on_cleanup)
        self._clients = set()
        self._setup_routes()
        self._lock = threading.Lock()
        self._sample = None
        self._last_rx = 0.0
        self._control_sequence = None
        self._events = collections.deque(maxlen=256)
        self._controls = relay.PhoneControls()
        self._framing = relay.SessionFraming(
            path=relay.frame_presets_path(), event_sink=print)
        self._freecam_options = relay.FreecamOptions()
        self._run_speed = relay.Toggle(relay.CONTROL_RUN)
        self._steadycam = relay.Toggle(relay.CONTROL_STEADYCAM)
        self._motion_fx = relay.Toggle(relay.CONTROL_MOTION_FX)
        self._walking_fx = relay.Toggle(relay.CONTROL_WALKING_FX)
        self._smoother = relay.PoseSmoother()
        self._joystick = relay.JoystickMotionSmoother()
        self._walking = relay.WalkingFx()
        self._motion = relay.VehicleMotionFx()
        self._requested_fov = 70.0
        self._gap_zoom = relay.GapZoom()
        self.gap_info = None
        self._player_track = playertrack.PlayerTrack()
        self.track_info = None
        self.pose_source = None
        self.cars_source = None
        self._active_before = False
        self._vehicle = None
        self._telemetry = None
        self._caps = {"drone_inertia": False, "sync_filter": True,
                      "steadycam": True}
        self.camera_target = None
        self.sequencer = None
        self._seq_was_engaged = False
        self._last_pose = None
        self._last_pose_context = {}

    OPEN_PATHS = ("/", "/config.json", "/pair")

    @web.middleware
    async def _require_pairing(self, request, handler):
        path = request.path
        if path in self.OPEN_PATHS or path.startswith("/static/"):
            return await handler(request)
        token = request.headers.get("X-HL-Token") or request.query.get("t", "")
        if token and token in self._tokens:
            self._tokens[token] = self._tokens.pop(token)
            return await handler(request)
        return web.json_response({"ok": False, "error": "not paired"}, status=401)

    @staticmethod
    def _make_code() -> str:
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        return "".join(secrets.choice(alphabet) for _ in range(8))

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/config.json", self.handle_config)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/pair", self.handle_pair)
        self.app.router.add_get("/pose", self.handle_pose_ws)
        self.app.router.add_post("/whep", self.handle_whep)
        self.app.router.add_post("/record", self.handle_record)
        self.app.router.add_post("/viewport", self.handle_viewport)
        self.app.router.add_post("/livefail", self.handle_livefail)
        self.app.router.add_post("/phonelog", self.handle_phonelog)
        self.app.router.add_post("/air", self.handle_air)
        self.app.router.add_get("/vista", self.handle_vista_status)
        self.app.router.add_post("/vista", self.handle_vista_press)
        self.app.router.add_post("/vista/setup", self.handle_vista_setup)
        self.app.router.add_get("/recordings", self.handle_recordings)
        self.app.router.add_get("/recordings/{name}", self.handle_recording_file)
        self.app.router.add_static("/static/", str(config.WEB_DIR), show_index=False)

    async def handle_index(self, request):
        index = config.WEB_DIR / "index.html"
        if not index.is_file():
            return web.Response(text="client not built", status=500)
        return web.FileResponse(index)

    async def handle_config(self, request):
        self._note_phone(request)
        tls_on = getattr(self, "_tls_on", False)
        ws_scheme = "wss" if tls_on else "ws"
        import hashlib
        import platform
        pc_name = platform.node() or "This PC"
        hub_id = hashlib.sha256(pc_name.encode("utf-8")).hexdigest()[:16]
        return web.json_response({
            "whepUrl": "/whep",
            "poseWs": f"{ws_scheme}://{request.host}/pose",
            "pairRequired": True,
            "pcName": pc_name,
            "hubId": hub_id,
            "game": _game_label(),
        })

    def _note_phone(self, request):
        hook = getattr(self, "baseline_hook", None)
        if hook:
            hook("android" in request.headers.get("User-Agent", "").lower())

    def phone_log(self, line: str):
        try:
            path = config.data_dir() / "phone.log"
            if path.is_file() and path.stat().st_size > 200_000:
                path.replace(path.with_suffix(".old.log"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {line[:600]}\n")
        except OSError:
            pass

    async def handle_phonelog(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)
        ua = request.headers.get("User-Agent", "")
        try:
            text = json.dumps(body)[:500]
        except (TypeError, ValueError):
            text = "(unreadable)"
        self.phone_log(f"[phone] {text} | {ua[:120]}")
        return web.json_response({"ok": True})

    async def handle_air(self, request):
        air = getattr(self, "air", None)
        if air is None:
            return web.json_response({"ok": False}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False}, status=400)
        if isinstance(body.get("settings"), dict):
            air.update(body["settings"])
        if body.get("action") in ("up", "down", "toggle"):
            air.command(body["action"])
        return web.json_response({"ok": True, "air": air.snapshot()})

    async def handle_vista_status(self, request):
        from . import vista
        loop = asyncio.get_running_loop()
        return web.json_response(dict(await loop.run_in_executor(None, vista.status), ok=True))

    async def handle_vista_press(self, request):
        from . import vista
        try:
            action = str((await request.json()).get("action", ""))
        except Exception:
            return web.json_response({"ok": False, "message": "bad request"}, status=400)
        loop = asyncio.get_running_loop()
        ok, message = await loop.run_in_executor(None, vista.press, action)
        return web.json_response({"ok": ok, "message": message})

    async def handle_vista_setup(self, request):
        from . import vista
        loop = asyncio.get_running_loop()
        ok, message = await loop.run_in_executor(None, vista.setup_bindings)
        return web.json_response({"ok": ok, "message": message})

    async def handle_whep(self, request):
        from aiohttp import ClientSession, ClientTimeout
        body = await request.read()
        self._note_phone(request)
        hook = getattr(self, "keyframe_hook", None)
        if hook:
            hook()
        _, whep_port = self.stream_whep_host_port
        url = f"http://127.0.0.1:{whep_port}/{config.STREAM_PATH}/whep"
        offered = sorted({l.split(" ", 1)[1].strip() for l in body.decode(errors="replace").splitlines()
                          if l.startswith("a=rtpmap:") and " " in l})
        try:
            async with ClientSession(timeout=ClientTimeout(total=8)) as s:
                async with s.post(url, data=body,
                                  headers={"Content-Type": "application/sdp"}) as r:
                    text = await r.text()
                    if r.status >= 300:
                        self.phone_log(f"[whep] {r.status} {text.strip()[:200]} | "
                                       f"phone offers {', '.join(offered)[:300]}")
                    else:
                        fmtp = [l for l in text.splitlines() if l.startswith("a=fmtp:")]
                        self.phone_log(f"[whep] {r.status} answer {' ; '.join(fmtp)[:250]}")
                    return web.Response(text=text, status=r.status,
                                        content_type="application/sdp")
        except Exception as error:
            self.phone_log(f"[whep] proxy failed: {error!r}")
            return web.Response(text="", status=502)

    async def handle_health(self, request):
        with self._lock:
            sample = dict(self._sample) if self._sample else None
            last_rx = self._last_rx
        return web.json_response({
            "ok": True,
            "companionLinked": self.link is not None,
            "switcher": (self.switcher.snapshot()
                         if getattr(self, "switcher", None) else None),
            "clients": len(self._clients),
            "lastPoseAgeMs": int((time.time() - last_rx) * 1000)
                             if last_rx else None,
            "lastSample": sample,
            "pipelineActive": self._active_before,
            "car": self._controls.car,
            "framePreset": relay.FRAME_NAMES[self._framing.selected],
            "frameOffset": [round(v, 4) for v in self._framing.offset],
            "run": self._run_speed.enabled,
            "steadycam": self._steadycam.enabled,
            "gap": self._gap_health() if self._active_before else None,
            "cameraTarget": self.camera_target,
            "air": (self.air.snapshot() if getattr(self, "air", None) else None),
            "recording": (bool(self.recording_state())
                          if getattr(self, "recording_state", None) else None),
            "streamGen": (self.stream_generation()
                          if getattr(self, "stream_generation", None) else None),
            "sequencer": (self.sequencer.snapshot()
                          if getattr(self, "sequencer", None) else None),
        })

    async def handle_record(self, request):
        cb = getattr(self, "record_toggle", None)
        if cb is None:
            return web.json_response(
                {"ok": False, "error": "recording unavailable"}, status=503)
        state_fn = getattr(self, "recording_state", None)
        was = bool(state_fn()) if state_fn else False
        loop = asyncio.get_running_loop()
        try:
            state = bool(await loop.run_in_executor(None, cb))
        except Exception:
            return web.json_response({"ok": False}, status=500)
        if not was and not state:
            return web.json_response({"ok": False, "error": "recording unavailable"}, status=503)
        return web.json_response({"ok": True, "recording": state})

    async def handle_viewport(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False}, status=400)
        if "quality" in body:
            hook = getattr(self, "quality_hook", None)
            if hook is None or body.get("quality") not in ("standard", "high", "max"):
                return web.json_response({"ok": False}, status=400)
            hook(body["quality"])
            return web.json_response({"ok": True})
        if "wide" in body:
            wide_hook = getattr(self, "wide_hook", None)
            wide = body.get("wide")
            if wide_hook is None or not (wide is None or (
                    isinstance(wide, list) and len(wide) == 2
                    and all(isinstance(v, int) for v in wide))):
                return web.json_response({"ok": False}, status=400)
            wide_hook(tuple(wide) if wide else None)
            return web.json_response({"ok": True})
        try:
            w, h = int(body.get("width", 0)), int(body.get("height", 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        hook = getattr(self, "viewport_hook", None)
        if hook is None or not (100 <= w <= 10000 and 100 <= h <= 10000):
            return web.json_response({"ok": False}, status=400)
        hook(w, h)
        return web.json_response({"ok": True})

    async def handle_livefail(self, request):
        hook = getattr(self, "livefail_hook", None)
        self.phone_log("[livefail] no picture after 8 s; trying the other capture path")
        if hook:
            hook()
        return web.json_response({"ok": True})

    async def handle_recordings(self, request):
        items = []
        for p in config.recordings_dir().glob("*.mp4"):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({"name": p.name, "sizeMb": round(st.st_size / 1e6, 1),
                          "modified": int(st.st_mtime)})
        items.sort(key=lambda i: i["modified"], reverse=True)
        return web.json_response({"ok": True, "recordings": items})

    async def handle_recording_file(self, request):
        name = request.match_info.get("name", "")
        folder = config.recordings_dir().resolve()
        path = (folder / name).resolve()
        if path.parent != folder or path.suffix.lower() != ".mp4" \
                or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{path.name}"'})

    async def handle_pair(self, request):
        code = (request.query.get("code") or "").strip().upper().replace("-", "")
        ok = secrets.compare_digest(code, self.pair_code)
        token = secrets.token_urlsafe(18) if ok else ""
        if token:
            while len(self._tokens) >= 64:
                self._tokens.pop(next(iter(self._tokens)))
            self._tokens[token] = time.time()
        return web.json_response({"ok": ok, "token": token})

    async def handle_pose_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(data, dict):
                        try:
                            self._ingest(data)
                        except Exception as error:
                            server_log(f"pose sample ignored: {error!r}")
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
        return ws

    @staticmethod
    def _finite(value, default=0.0):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        return v if math.isfinite(v) else default

    def _ingest(self, data: dict):
        euler = data.get("euler")
        if not (isinstance(euler, (list, tuple)) and len(euler) == 3):
            euler = (0.0, 0.0, 0.0)
        euler = tuple(self._finite(v) for v in euler)
        movement = data.get("movement")
        if not (isinstance(movement, (list, tuple)) and len(movement) == 3):
            movement = (0.0, 0.0, 0.0)
        movement = tuple(self._finite(v) for v in movement)
        position = data.get("position")
        if isinstance(position, (list, tuple)) and len(position) == 3:
            position = tuple(max(-20.0, min(20.0, self._finite(v))) for v in position)
        else:
            position = None
        sample = {
            "euler": euler,
            "tracking": bool(data.get("tracking", True)),
            "freecam": bool(data.get("freecam", False)),
            "zoom": (self._finite(data.get("zoom"), None)
                     if isinstance(data.get("zoom"), (int, float)) else None),
            "position": position,
            "fov_base": max(40.0, min(110.0, self._finite(data.get("fov_base"), 70.0))),
            "gap_dynamic": data.get("gap_dynamic") is True,
            "gap_track": data.get("gap_track") is True,
            "gap_track_range": self._finite(data.get("gap_track_range"), playertrack.RANGE_DEFAULT_M),
            "gap_track_smooth": (data["gap_track_smooth"]
                                 if isinstance(data.get("gap_track_smooth"), str)
                                 and data["gap_track_smooth"] in playertrack.SMOOTH_SETTLE_S
                                 else playertrack.SMOOTH_DEFAULT),
            "gap_zoom_start": self._finite(data.get("gap_zoom_start"), relay.GAP_START_MPH),
            "gap_zoom_out": self._finite(data.get("gap_zoom_out"), relay.GAP_MAX_OUT),
        }
        control_sequence = data.get("control_sequence")
        now = time.time()
        with self._lock:
            self._sample = sample
            self._last_rx = now
            if isinstance(control_sequence, int) \
                    and control_sequence != self._control_sequence:
                self._control_sequence = control_sequence
                try:
                    control_id = int(data.get("control_id", 0))
                except (TypeError, ValueError):
                    control_id = 0
                self._events.append(
                    (control_id, self._finite(data.get("value")), movement))

    def _send_loop(self):
        tick = 0
        while not self._stopped.wait(1.0 / self.SEND_HZ):
            tick += 1
            try:
                self._send_tick(tick)
            except Exception as error:
                print(f"HorizonLens relay tick failed: {error!r}")

    STATUS_SECONDS = 1.0 / 6

    def _status_loop(self):
        while not self._stopped.wait(self.STATUS_SECONDS):
            link = self.link
            if link is None:
                continue
            try:
                st = link.status(timeout=0.3)
            except Exception:
                st = None
            if st:
                self._status = (time.monotonic(), st)
                try:
                    self._ingest_status(st)
                except Exception:
                    pass

    def fresh_status(self, max_age=0.5):
        got = getattr(self, "_status", None)
        if got and time.monotonic() - got[0] <= max_age:
            return got[1]
        return None

    def _send_tick(self, tick):
        now = time.time()
        with self._lock:
            sample = self._sample
            last_rx = self._last_rx
            events = list(self._events)
            self._events.clear()
        phone_active = (sample is not None and bool(self._clients)
                        and sample["tracking"]
                        and now - last_rx <= self.STALE_HOLD_SECONDS)
        active = phone_active and self.link is not None

        controls = self._controls
        if sample is not None:
            controls.set_freecam(sample["freecam"])
        for control_id, value, movement in events:
            controls.control(control_id, value, movement, now)
            self._framing.control(control_id, value, movement, controls.car)
            self._freecam_options.control(control_id, value)
            self._run_speed.control(control_id, value)
            self._steadycam.control(control_id, value)
            self._motion_fx.control(control_id, value)
            self._walking_fx.control(control_id, value)
        self._motion.settings["enabled"] = self._motion_fx.enabled
        self._walking.settings = (self._walking_fx.enabled,) + self._walking.settings[1:]
        car = controls.car
        self.camera_target = (relay.desired_camera(car, self._framing.selected)
                              if phone_active else None)

        seq = getattr(self, "sequencer", None)
        seq_engaged = bool(seq is not None and self.link is not None
                           and seq.is_engaged())
        if seq_engaged:
            self._seq_was_engaged = True
            self._active_before = False
            self._send_sequencer_pose(now)
            return
        if self._seq_was_engaged:
            self._seq_was_engaged = False
            self._smoother.reset()
            if not active and self.link is not None:
                kind = 7
                try:
                    self.link.send_pose(rcf6.Pose(kind=kind))
                except Exception:
                    pass

        if active:
            self._active_before = True
            self._send_pose(sample, last_rx, now, car)
        else:
            if self._active_before:
                if self.link is not None:
                    kind = 7
                    try:
                        self.link.send_pose(rcf6.Pose(kind=kind))
                    except Exception:
                        pass
                self._smoother.reset()
                self._joystick.reset()
                self._walking.reset()
                self._motion.reset()
                self._active_before = False
            self._framing.pause()

    def _send_pose(self, sample, last_rx, now, car):
        controls = self._controls
        self._requested_fov = relay.forza_fov(sample["zoom"], self._requested_fov,
                                              base=sample.get("fov_base", relay.NATIVE_FOV))
        rotation = relay.phone_rotation(sample["euler"], controls.yaw)
        self._framing.update(now, car, relay.level_heading(rotation))
        drone_inertia = (not car and self._freecam_options.drone_inertia
                         and self._caps["drone_inertia"])
        movement = self._joystick.update(
            relay.scale_forza_freecam_movement(
                controls.held_movement(now, last_rx),
                self._run_speed.enabled, drone=drone_inertia),
            now, temporal_smoothing=not drone_inertia)
        vehicle = self._vehicle or {}
        seat_x = vehicle.get("seat_x")
        if seat_x is None:
            seat_x = relay.PASSENGER_X_FALLBACK
        options = self._freecam_options.wire_options(
            self._caps["drone_inertia"], self._caps["sync_filter"],
            self._caps["steadycam"], self._steadycam.enabled)
        car_anchor = self._framing.car_anchor(
            seat_x, relay.PASSENGER_Y, relay.PASSENGER_Z,
            vehicle.get("dims", relay.FALLBACK_DIMS))
        position = sample.get("position")
        output = relay.translate(
            car, rotation, controls.reset, self._requested_fov,
            movement if not car else (0.0, 0.0, 0.0), options,
            self._freecam_options.spawn_offset(seat_x), car_anchor,
            position=position or (0.0, 0.0, 0.0))
        walking_active, motion_active = relay.fx_activity(
            car, drone_inertia, self._steadycam.enabled)
        output = relay.apply_fx(output, self._walking.update(
            movement, now, active=walking_active))
        output = relay.apply_fx(output, self._motion.update(
            self._telemetry, now, free_camera=not car, active=motion_active))
        if car and position is None:
            output = self._neck_pivot(output)
        output = self._track_players(output, sample, car, now)
        output = self._dynamic_gap(output, sample, car, now, vehicle)
        sm = self._smoother.update(output, now,
                                   smooth_pose=not self._caps["sync_filter"])
        pose = rcf6.Pose(
            kind=1,
            mode=sm["mode"],
            options=sm["options"],
            sequence=0,
            recenter=sm["recenter"],
            xyz=tuple(sm["xyz"]),
            quat=tuple(sm["quaternion"]),
            fov=max(10.0, min(120.0, sm["fov"])),
            move=tuple(sm["move"]),
        )
        self._last_pose = {
            "xyz": tuple(sm["xyz"]), "quaternion": tuple(sm["quaternion"]),
            "fov": float(pose.fov), "mode": sm["mode"]}
        self._last_pose_context = {
            "car": bool(car),
            "camera_target": relay.desired_camera(car, self._framing.selected),
            "frame_preset": relay.FRAME_NAMES[self._framing.selected]}
        try:
            self.link.send_pose(pose)
        except Exception:
            pass

    def _send_sequencer_pose(self, now):
        seq = self.sequencer
        out = seq.sample()
        if out is None:
            return
        self.camera_target = out.get("camera_target") or "follow_low"
        xyz, quat = tuple(out["pos"]), tuple(out["quat"])
        pose = rcf6.Pose(kind=1, mode=0, options=0, sequence=0, recenter=self._controls.reset,
                         xyz=xyz, quat=quat, fov=max(10.0, min(120.0, out["fov"])),
                         move=(0.0, 0.0, 0.0))
        self._last_pose = {"xyz": xyz, "quaternion": quat, "fov": float(pose.fov), "mode": 0}
        self._last_pose_context = {"car": True, "camera_target": self.camera_target,
                                   "frame_preset": "sequencer"}
        try:
            self.link.send_pose(pose)
        except Exception:
            pass

    def speed_mps(self):
        source = getattr(self, "speed_source", None)
        if source is not None:
            try:
                v = source()
            except Exception:
                v = None
            if v is not None:
                return v
        t = self._telemetry
        if t and time.time() - t.get("received_at", 0.0) <= 1.0:
            return abs(t.get("speed", 0.0))
        return None

    def _dynamic_gap(self, output, sample, car, now, vehicle):
        if not (car and sample.get("gap_dynamic")
                and self._framing.selected == relay.FRAME_GAP):
            self._gap_zoom.reset()
            self.gap_info = None
            return output
        speed = self.speed_mps()
        mph = (speed or 0.0) * relay.MPS_TO_MPH
        base = relay.phone_zoom_to_factor(sample["zoom"]) if isinstance(
            sample.get("zoom"), (int, float)) else 1.0
        curve = relay.gap_speed_curve(mph, sample.get("gap_zoom_start", relay.GAP_START_MPH),
                                      sample.get("gap_zoom_out", relay.GAP_MAX_OUT))
        factor = self._gap_zoom.update(base * curve, now)
        fov, pull = relay.gap_lens(factor, sample.get("fov_base", relay.NATIVE_FOV),
                                   vehicle.get("dims", relay.FALLBACK_DIMS)[2])
        self.gap_info = {"zoom": round(factor, 2), "mph": round(mph) if speed is not None else None}
        return relay.pull_back(dict(output, fov=fov), pull)

    def _gap_health(self):
        if self.gap_info is None and self.track_info is None:
            return None
        return dict(self.gap_info or {}, track=self.track_info,
                    tracking=self._player_track_on)

    _player_track_on = False

    def _track_players(self, output, sample, car, now):
        on = bool(car and sample.get("gap_track") and self._framing.selected == relay.FRAME_GAP
                  and self.pose_source is not None and self.cars_source is not None)
        self._player_track_on = on
        if not on:
            self._player_track.reset()
            self.track_info = None
            return output
        track = self._player_track
        track.configure(sample.get("gap_track_range"), sample.get("gap_track_smooth"))
        try:
            me, cars = self.pose_source(), self.cars_source()
        except Exception:
            me, cars = None, []
        quat = track.update(now, output["quaternion"], output["xyz"], me, cars or [])
        self.track_info = dict(track.info, range=round(track.range_m)) if track.info             else {"name": None, "range": round(track.range_m)}
        return dict(output, quaternion=quat)

    @staticmethod
    def _neck_pivot(output):
        pz = -0.15
        qx, qy, qz, qw = output["quaternion"]
        rx = 2.0 * (qx * qz + qw * qy) * pz
        ry = 2.0 * (qy * qz - qw * qx) * pz
        rz = (1.0 - 2.0 * (qx * qx + qy * qy)) * pz
        x, y, z = output["xyz"]
        return dict(output, xyz=(x - rx, y - ry, z + pz - rz))

    def _ingest_status(self, st):
        lat = st.get("driver_lateral_m")
        seat_x = None
        if st.get("exterior_calibrated") is True \
                and isinstance(lat, (int, float)) \
                and math.isfinite(lat) and 0.15 <= abs(lat) <= 0.75:
            seat_x = max(-1.5, min(1.5, -2.0 * float(lat)))
        dims = tuple(st.get(k) for k in (
            "vehicle_width_m", "vehicle_height_m", "vehicle_length_m"))
        if all(isinstance(d, (int, float)) and math.isfinite(d)
               for d in dims) \
                and 0.5 <= dims[0] <= 5 and 0.3 <= dims[1] <= 4 \
                and 1 <= dims[2] <= 15:
            dims = tuple(float(d) for d in dims)
        else:
            dims = relay.FALLBACK_DIMS
        self._vehicle = {"seat_x": seat_x, "dims": dims}
        for key, field in (("drone_inertia", "freecam_drone_inertia_supported"),
                           ("sync_filter", "synchronized_pose_filter_supported"),
                           ("steadycam", "steadycam_supported")):
            value = st.get(field)
            if isinstance(value, bool):
                self._caps[key] = value
        speed = st.get("derived_speed_mps")
        ax = st.get("derived_acceleration_x")
        ay = st.get("derived_acceleration_y")
        az = st.get("derived_acceleration_z")
        accel = None
        if all(isinstance(v, (int, float)) for v in (ax, ay, az)):
            accel = (float(ax), float(ay), float(az))
        if isinstance(speed, (int, float)) or accel is not None:
            seq = st.get("derived_telemetry_sequence")
            if not isinstance(seq, int):
                seq = getattr(self, "_telemetry_seq", 0) + 1
                self._telemetry_seq = seq
            self._telemetry = {
                "active": True,
                "received_at": time.time(),
                "sequence": seq,
                "speed": float(speed) if isinstance(speed, (int, float))
                         else 0.0,
                "acceleration": accel if accel is not None
                                else (0.0, 0.0, 0.0),
            }

    WATCHDOG_SECONDS = 15.0
    WATCHDOG_TIMEOUT = 3.0

    def _listen_watchdog(self, loop, port):
        import socket
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        fails = 0
        while not self._stopped.wait(self.WATCHDOG_SECONDS):
            if not loop.is_running():
                return
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=self.WATCHDOG_TIMEOUT) as raw:
                    if self._tls_on:
                        raw.settimeout(self.WATCHDOG_TIMEOUT)
                        with ctx.wrap_socket(raw, server_hostname="horizonlens"):
                            pass
                fails = 0
            except OSError as error:
                fails += 1
                server_log(f"self-check failed ({fails}): {error!r}")
            if fails >= 2:
                fails = 0
                server_log("web server stopped accepting: opening it again")
                try:
                    asyncio.run_coroutine_threadsafe(self._reopen_site(), loop).result(timeout=15)
                    server_log("web server reopened")
                except Exception as error:
                    server_log(f"reopening failed: {error!r}")

    async def _reopen_site(self):
        runner, host, port, ssl_ctx = self._site_args
        try:
            await self._site.stop()
        except Exception:
            pass
        self._site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
        await self._site.start()

    async def run(self, host: str = "0.0.0.0", port: int | None = None):
        port = port or config.WEB_PORT
        runner = web.AppRunner(self.app)
        await runner.setup()
        ssl_ctx = None
        try:
            import ssl as _ssl
            cert_p, key_p = tls.ensure_cert()
            ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(str(cert_p), str(key_p))
        except Exception:
            ssl_ctx = None
        self._tls_on = ssl_ctx is not None
        site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
        await site.start()
        self._site_args = (runner, host, port, ssl_ctx)
        self._site = site
        threading.Thread(target=self._status_loop, daemon=True,
                         name="hl-companion-status").start()
        threading.Thread(target=self._listen_watchdog, args=(asyncio.get_running_loop(), port),
                         daemon=True, name="hl-listen-watchdog").start()
        threading.Thread(target=self._send_loop, daemon=True,
                         name="hl-pose-send").start()
        return runner
