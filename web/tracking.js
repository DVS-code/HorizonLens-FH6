(function () {
  const D2R = Math.PI / 180, R2D = 180 / Math.PI;

  function qMul(a, b) {
    return [
      a[3] * b[0] + a[0] * b[3] + a[1] * b[2] - a[2] * b[1],
      a[3] * b[1] - a[0] * b[2] + a[1] * b[3] + a[2] * b[0],
      a[3] * b[2] + a[0] * b[1] - a[1] * b[0] + a[2] * b[3],
      a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2],
    ];
  }
  function qAxis(ax, ay, az, angle) {
    const s = Math.sin(angle / 2);
    return [ax * s, ay * s, az * s, Math.cos(angle / 2)];
  }
  const Q_CAMERA = qAxis(1, 0, 0, -Math.PI / 2);

  function yxzEuler(q) {
    const n = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
    const x = q[0] / n, y = q[1] / n, z = q[2] / n, w = q[3] / n;
    const pitch = Math.asin(Math.max(-1, Math.min(1, 2 * (w * x - z * y))));
    const yaw = Math.atan2(2 * (w * y + x * z), 1 - 2 * (x * x + y * y));
    const roll = Math.atan2(2 * (w * z + x * y), 1 - 2 * (x * x + z * z));
    return [pitch * R2D, yaw * R2D, roll * R2D];
  }
  const wrap = (a) => ((a + 180) % 360 + 360) % 360 - 180;

  class OneEuro {
    constructor(minCutoff, beta, dCutoff) {
      this.minCutoff = minCutoff; this.beta = beta; this.dCutoff = dCutoff;
      this.x = null; this.dx = 0; this.t = 0;
    }
    static alpha(cutoff, dt) { const r = 2 * Math.PI * cutoff * dt; return r / (r + 1); }
    reset() { this.x = null; this.dx = 0; }
    filter(value, t) {
      if (this.x === null) { this.x = value; this.t = t; return value; }
      const dt = Math.max(1e-3, Math.min(0.1, t - this.t)); this.t = t;
      const dx = (value - this.x) / dt;
      this.dx += (dx - this.dx) * OneEuro.alpha(this.dCutoff, dt);
      const cutoff = this.minCutoff + this.beta * Math.abs(this.dx);
      this.x += (value - this.x) * OneEuro.alpha(cutoff, dt);
      return this.x;
    }
  }

  const Tracking = {
    _on: false,
    _raw: null,
    _baseYaw: null,
    _lastYaw: null, _yawTurns: 0,
    _filters: [new OneEuro(0.7, 0.012, 1.0), new OneEuro(0.7, 0.012, 1.0), new OneEuro(0.7, 0.012, 1.0)],
    _posFilters: [new OneEuro(1.2, 2.0, 1.0), new OneEuro(1.2, 2.0, 1.0), new OneEuro(1.2, 2.0, 1.0)],
    events: 0,

    _xr: null, _xrStarting: null,
    _pos: null,
    _origin: null,
    positionOk: false,
    xrError: null,
    onXRChange: null,

    async enableOrientation() {
      const DOE = window.DeviceOrientationEvent;
      if (DOE && typeof DOE.requestPermission === "function") {
        try { if (await DOE.requestPermission() !== "granted") return false; }
        catch (e) { return false; }
      }
      if (!this._on) {
        window.addEventListener("deviceorientation", (e) => this._onOrient(e), true);
        this._on = true;
      }
      return true;
    },

    _onOrient(e) {
      if (this._xr || this._native || e.alpha == null) return;
      this.events++;
      let q = qMul(qMul(qAxis(0, 1, 0, e.alpha * D2R), qAxis(1, 0, 0, (e.beta || 0) * D2R)),
                   qAxis(0, 0, 1, -(e.gamma || 0) * D2R));
      q = qMul(q, Q_CAMERA);
      const so = (screen.orientation && screen.orientation.angle) || window.orientation || 0;
      if (so) q = qMul(q, qAxis(0, 0, 1, -so * D2R));
      this._ingest(q);
    },

    _ingest(q) {
      const [pitch, yaw, roll] = yxzEuler(q);

      if (this._lastYaw !== null) {
        const d = yaw - this._lastYaw;
        if (d > 180) this._yawTurns -= 360; else if (d < -180) this._yawTurns += 360;
      }
      this._lastYaw = yaw;
      this._raw = [pitch, yaw + this._yawTurns, roll];
      if (this._baseYaw === null) this._baseYaw = this._raw[1];
    },

    _feedPosition(p) {
      if (!p) { this.positionOk = false; this._resume = true; return; }
      if (this._lastRaw && this._origin) {
        const jump = Math.hypot(p[0] - this._lastRaw[0], p[1] - this._lastRaw[1], p[2] - this._lastRaw[2]);
        if (this._resume || jump > 0.3) {
          for (let i = 0; i < 3; i++) this._origin[i] += p[i] - this._lastRaw[i];
        }
      }
      this._resume = false;
      this.positionOk = true;
      this._lastRaw = p;
      this._pos = p;
      if (!this._origin) this._origin = p.slice();
    },

    _restart() {
      this._raw = null; this._lastYaw = null; this._yawTurns = 0; this._baseYaw = null;
      this._pos = null; this._origin = null; this.positionOk = false;
      this._lastRaw = null; this._resume = false; this._held = null; this._prevRel = null;
      this._filters.forEach((f) => f.reset());
      this._posFilters.forEach((f) => f.reset());
    },

    native: !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.horizonlens),
    _native: false,
    nativePose(qx, qy, qz, qw, px, py, pz, ok) {
      if (!this._native) {
        this._restart(); this._native = true;
        if (this.onXRChange) this.onXRChange(true);
      }
      this.events++;
      this._ingest([qx, qy, qz, qw]);
      this._feedPosition(ok ? [px, py, pz] : null);
    },
    nativeStopped() {
      if (!this._native) return;
      this._native = false; this._restart();
      if (this.onXRChange) this.onXRChange(false);
    },

    async xrSupported() {
      try { return !!(navigator.xr && await navigator.xr.isSessionSupported("immersive-ar")); }
      catch (e) { return false; }
    },
    get xrActive() { return !!(this._xr || this._native); },

    startXR(root) {
      if (this._xr) return Promise.resolve(true);
      if (this._xrStarting) return this._xrStarting;
      this._xrStarting = this._startXR(root).finally(() => { this._xrStarting = null; });
      return this._xrStarting;
    },
    async _startXR(root) {
      this.xrError = null;
      let session = null;
      try {
        if (!navigator.xr) throw new Error("WebXR not available");
        session = await navigator.xr.requestSession("immersive-ar", {
          requiredFeatures: ["local"],
          optionalFeatures: ["dom-overlay"],
          domOverlay: { root },
        });

        if (!session.domOverlayState) throw new Error("this phone can't show the page over AR");
        const canvas = document.createElement("canvas");
        const gl = canvas.getContext("webgl", { xrCompatible: true, antialias: false, alpha: true });
        if (!gl) throw new Error("WebGL unavailable");
        await session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl, { antialias: false }) });
        const ref = await session.requestReferenceSpace("local");
        this._restart();
        this._xr = session;
        session.addEventListener("end", () => {
          this._xr = null;
          this._restart();
          if (this.onXRChange) this.onXRChange(false);
        });
        const onFrame = (t, frame) => {
          if (!this._xr) return;
          session.requestAnimationFrame(onFrame);

          const layer = session.renderState.baseLayer;
          gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
          gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
          const pose = frame.getViewerPose(ref);
          if (!pose) { this._feedPosition(null); return; }
          const o = pose.transform.orientation, p = pose.transform.position;
          this.events++;
          this._ingest([o.x, o.y, o.z, o.w]);

          this._feedPosition(pose.emulatedPosition ? null : [p.x, p.y, p.z]);
        };
        session.requestAnimationFrame(onFrame);
        if (this.onXRChange) this.onXRChange(true);
        return true;
      } catch (e) {
        this.xrError = (e && (e.message || e.name)) || String(e);
        if (session) { try { await session.end(); } catch (x) {} }
        this._xr = null;
        return false;
      }
    },
    async stopXR() {
      if (this._xr) { try { await this._xr.end(); } catch (e) {} }
    },

    recenter() {
      if (this._raw) this._baseYaw = this._raw[1];
      if (this._pos) this._origin = this._pos.slice();
      this._held = null; this._prevRel = null;
      this._filters.forEach((f) => f.reset());
      this._posFilters.forEach((f) => f.reset());
    },

    isLive() { return (this._on || this._xr || this._native) && this.events > 0 && this._raw !== null; },

    getPose() {
      const tracking = this.isLive();
      if (!tracking) return { euler: [0, 0, 0], tracking: false, position: null };
      const t = performance.now() / 1000;
      const pitch = this._filters[0].filter(this._raw[0], t);
      const yaw = this._filters[1].filter(this._raw[1] - this._baseYaw, t);
      const roll = this._filters[2].filter(this._raw[2], t);
      const r = (v) => Math.round(v * 1000) / 1000;
      let position = null;
      if ((this._xr || this._native) && this._pos && this._origin) {

        const h = this._baseYaw * D2R, c = Math.cos(h), s = Math.sin(h);
        const dx = this._pos[0] - this._origin[0], dy = this._pos[1] - this._origin[1],
              dz = this._pos[2] - this._origin[2];
        const rel = [dx * c - dz * s, dy, dx * s + dz * c];
        const f = rel.map((v, i) => this._posFilters[i].filter(v, t));

        if (!this._held || !this._prevRel) {
          this._held = f.slice();
        } else {
          const dt = Math.max(1e-3, t - this._prevT);
          const speed = Math.hypot(f[0] - this._prevRel[0], f[1] - this._prevRel[1], f[2] - this._prevRel[2]) / dt;
          if (speed > 0.04) for (let i = 0; i < 3; i++) this._held[i] += f[i] - this._prevRel[i];
        }
        this._prevRel = f; this._prevT = t;
        position = this._held.map((v) => r(Math.max(-20, Math.min(20, v))));
      }
      return { euler: [r(pitch), r(wrap(yaw)), r(roll)], tracking: true, position };
    },
  };

  window.HLTracking = Tracking;
})();
