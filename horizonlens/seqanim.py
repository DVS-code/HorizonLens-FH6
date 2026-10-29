from __future__ import annotations

import threading
import time

from . import vista

TICK_SECONDS = 0.03
SETTLE_SECONDS = 0.3
RETRY_SECONDS = 3.0


class AnimationDriver:
    def __init__(self, engine, press=None, clock=time.monotonic, air=None):
        self.engine = engine
        self.air = air
        self._air_on = False
        self._press = press or (lambda action: vista.press(action, fast=True))
        self._clock = clock
        self.applied: dict[str, bool] = {}
        self._last_t = None
        self._pending = None
        self._pending_at = 0.0
        self._blocked_until = 0.0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hl-seq-anim")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self.stop_air()

    def _run(self):
        from . import game
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick(game.game_pid_cached(2.0))
                if self.last_error.startswith("worker:"):
                    self.last_error = ""
            except Exception as error:
                self.last_error = f"worker: {error!r}"[:120]

    def forget(self):
        with self._lock:
            self.applied.clear()
            self._pending = None

    def reset_car(self):
        ok, message = self._press("resetstate")
        self.forget()
        if not ok:
            self.last_error = message
        return ok, message

    def snapshot(self):
        with self._lock:
            return {a: v for a, v in self.applied.items() if v}

    def _drive_air(self, eng):
        air = self.air
        if air is None:
            return
        engaged = eng.is_engaged()
        state = eng.air_state() if engaged else None
        if state is None:
            if self._air_on:
                self._air_on = False
                keep = not engaged and not eng.seq.settings["air"]["return_on_stop"]
                air.release(ramp_back=not keep)
            return
        heights, cambers = state
        air.drive(eng.seq.settings["air"], heights, cambers)
        self._air_on = True

    def stop_air(self):
        if self._air_on and self.air is not None:
            self._air_on = False
            self.air.release(ramp_back=True)

    def tick(self, game_pid=None):
        eng = self.engine
        now = self._clock()
        if game_pid is not None and game_pid != getattr(self, "_game_pid", game_pid):
            self.forget()
        if game_pid is not None:
            self._game_pid = game_pid
        self._drive_air(eng)
        if not eng.is_engaged() or not eng.seq.triggers:
            self._last_t = None
            self._pending = None
            return
        t = eng.time()
        playing = eng.state == "playing"
        presses = []
        if playing and self._last_t is not None and t >= self._last_t:
            presses += [(a, None) for a in eng.seq.one_shots_between(self._last_t, t)]
        self._last_t = t
        want = eng.animation_state(t)
        with self._lock:
            if not playing:
                if want != self._pending:
                    self._pending, self._pending_at = dict(want), now
                if now - self._pending_at < SETTLE_SECONDS:
                    want = None
            else:
                self._pending = None
            if want is not None:
                for action in sorted(set(want) | set(self.applied)):
                    on = want.get(action, False)
                    if on != self.applied.get(action, False):
                        presses.append((action, on))
        if not presses or now < self._blocked_until:
            return
        for action, on in presses:
            ok, message = self._press(action)
            if not ok:
                self.last_error = message
                self._blocked_until = now + RETRY_SECONDS
                try:
                    eng.message = f"Animation: {message}"
                    eng._notify()
                except Exception:
                    pass
                return
            self.last_error = ""
            with self._lock:
                if on is None:
                    self.applied.clear()
                else:
                    self.applied[action] = on
