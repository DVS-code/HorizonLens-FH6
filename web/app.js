(function () {
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k, d) { try { const v = localStorage.getItem("hl_" + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("hl_" + k, JSON.stringify(v)); } catch (e) {} },
  };

  const PRESETS = [
    { id: 1, name: "PASSENGER" },
    { id: 5, name: "DRIVER" },
    { id: 2, name: "WINDOW" },
    { id: 3, name: "BUMPER" },
    { id: 4, name: "WHEEL" },
    { id: 6, name: "GAP" },
  ];

  const PRESET_DEFAULT_ZOOM = { 6: 0.5 };
  const C = { RECENTER: 2, MODE: 3, FREE_MOVE: 4, PRESET: 5, NUDGE: 7, SAVE: 8,
              OPTIONS: 15, RUN: 23, STEADY: 24, MOTION_FX: 90, WALKING_FX: 91 };
  const PHONE_SPEED_MAXIMUM = 5.0;

  let cfg = null, ws = null, pc = null;

  let hlToken = "";
  const rawFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => {
    if (typeof url === "string" && url.startsWith("/") && hlToken) {
      opts = Object.assign({}, opts);
      opts.headers = Object.assign({}, opts.headers || {}, { "X-HL-Token": hlToken });
    }
    return rawFetch(url, opts).then((r) => {
      if (r.status === 401 && hlToken && typeof url === "string" && url.startsWith("/")) pairingExpired();
      return r;
    });
  };

  function pairingExpired() {
    if (pairingExpired.done) return;
    pairingExpired.done = true;
    store.set("code", "");
    location.href = location.pathname + "?expired=1";
  }
  let freecam = store.get("freecam", false);
  let preset = store.get("preset", 1);
  let adjust = store.get("adjust", true);
  let zoomFactor = 1.0;
  const opts = Object.assign(
    { run: false, steadycam: false, drone: false, standUp: true, ignoreGround: false,
      motionFx: false, walkingFx: false, fovBase: 70, moveScale: 1.5, zoomRail: true },
    store.get("opts", {}));

  let ctrl = { seq: 0, id: 0, value: 0, movement: [0, 0, 0] };
  const pad = [0, 0];
  let lift = 0;
  let stickDirty = false, stickWasMoving = false;

  function toast(msg) {
    const t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 1500);
  }
  const presetName = (id) => (PRESETS.find((p) => p.id === id) || PRESETS[0]).name;

  function normalizedZoom() {
    const f = Math.max(0.5, Math.min(3, zoomFactor));
    return f <= 1 ? (f - 0.5) / 0.5 * 0.2 : 0.2 + (f - 1) / 2 * 0.8;
  }

  const WIDE_STEPS = [
    { f: 0.5, a: null }, { f: 0.38, a: [3, 4] }, { f: 0.28, a: [1, 1] },
    { f: 0.21, a: [4, 3] }, { f: 0.16, a: [16, 9] },
  ];
  const wideAllowed = () => !freecam && preset === 6 && window.innerHeight >= window.innerWidth;
  let wideSent = "null";
  function requestWide(aspect) {
    const want = JSON.stringify(aspect);
    clearTimeout(requestWide._t);
    requestWide._t = setTimeout(() => {
      if (want === wideSent) return;
      wideSent = want;
      expectStreamChange();
      fetch("/viewport", { method: "POST", headers: { "Content-Type": "application/json" },
                           body: JSON.stringify({ wide: aspect }) }).catch(() => {});
    }, 400);
  }
  function setZoom(f, show) {
    zoomFactor = Math.max(wideAllowed() ? 0.16 : 0.5, Math.min(3, f));
    let wide = null;
    if (zoomFactor < 0.5) {
      const step = WIDE_STEPS.reduce((b, s) => Math.abs(s.f - zoomFactor) < Math.abs(b.f - zoomFactor) ? s : b);
      zoomFactor = step.f; wide = step.a;
    }
    requestWide(wide);
    if (!freecam && typeof zoomFor !== "undefined") { zoomFor[preset] = zoomFactor; store.set("zoomFor", zoomFor); }
    const label = (zoomFactor < 0.5 ? zoomFactor.toFixed(2) : zoomFactor.toFixed(1)) + "×";
    $("zoomLabel").textContent = label; $("optZoomV").textContent = label;
    $("optZoom").min = wideAllowed() ? 0.16 : 0.5;
    $("optZoom").value = zoomFactor;
    renderZoomRail();
    if (show) {
      const r = $("reticle"); r.classList.add("show");
      clearTimeout(r._h); r._h = setTimeout(() => r.classList.remove("show"), 900);
    }
  }

  const railMin = () => (wideAllowed() ? 0.16 : 0.5);
  const railPos = (f) => Math.log(f / railMin()) / Math.log(3 / railMin());
  function renderZoomRail() {
    const rail = $("zoomRail");
    if (!rail) return;
    const t = Math.max(0, Math.min(1, railPos(Math.max(railMin(), zoomFactor))));
    rail.style.setProperty("--t", t.toFixed(4));
  }
  function bindZoomRail() {
    const rail = $("zoomRail"), track = rail.querySelector(".track");
    let dragging = false;
    const fromY = (y) => {
      const r = track.getBoundingClientRect();
      const t = Math.max(0, Math.min(1, (r.bottom - y) / r.height));
      setZoom(railMin() * Math.pow(3 / railMin(), t), true);
    };
    rail.addEventListener("pointerdown", (e) => {
      dragging = true; rail.classList.add("drag"); rail.setPointerCapture(e.pointerId); fromY(e.clientY);
      e.preventDefault();
    });
    rail.addEventListener("pointermove", (e) => { if (dragging) fromY(e.clientY); });
    const end = () => { dragging = false; rail.classList.remove("drag"); };
    rail.addEventListener("pointerup", end);
    rail.addEventListener("pointercancel", end);
    rail.addEventListener("dblclick", () => { setZoom(1, true); toast("Zoom 1.0×"); });
    renderZoomRail();
  }

  let lockLook = null, lockPos = null;
  const scaledPosition = (p) => p.position && p.position.map((v) => Math.round(v * opts.moveScale * 1000) / 1000);
  function setLock(on) {
    if (on) {
      const p = window.HLTracking.getPose();
      lockLook = p.tracking ? p.euler.slice() : [0, 0, 0];
      lockPos = scaledPosition(p) || null;
    } else lockLook = lockPos = null;
    renderLock();
  }
  function renderLock() {
    renderGap();
  }

  const gapPrefs = Object.assign({ static: false, dynamic: false, track: false, look: null,
    start: 100, out: 0.3, range: 60, smooth: "medium" }, store.get("gap", {}));
  let gapInfo = null;
  const gapStaticActive = () => gapPrefs.static && !freecam && preset === 6;
  function gapSave() { store.set("gap", gapPrefs); renderGap(); }
  function gapFreeze() {
    const p = window.HLTracking.getPose();
    gapPrefs.look = p.tracking ? p.euler.slice() : [0, 0, 0];
  }
  function renderGap() {
    $("swGapStatic").classList.toggle("on", gapPrefs.static);
    $("gapMore").classList.toggle("hidden", !gapPrefs.static);
    $("swGapDynamic").classList.toggle("on", gapPrefs.dynamic);
    $("swGapTrack").classList.toggle("on", gapPrefs.track);
    $("gapLockRow").classList.toggle("hidden", !gapPrefs.static);
    $("gapDynMore").classList.toggle("hidden", !gapPrefs.dynamic);
    $("gapTrackMore").classList.toggle("hidden", !gapPrefs.track);
    $("gapStart").value = gapPrefs.start; $("gapStartV").textContent = `${gapPrefs.start} mph`;
    $("gapOut").value = gapPrefs.out; $("gapOutV").textContent = `${(+gapPrefs.out).toFixed(2)}× at 250 mph`;
    $("gapRange").value = gapPrefs.range; $("gapRangeV").textContent = `${gapPrefs.range} m`;
    document.querySelectorAll("#gapSmooth button").forEach((b) => b.classList.toggle("on", b.dataset.v === gapPrefs.smooth));
    const live = vidOn || camOwned === true;
    $("btnLock").classList.toggle("hidden", !live || gapStaticActive());
    $("btnLock").classList.toggle("on", !!lockLook);
    ["btnLock", "zoomRail"].forEach((id) => $(id).classList.toggle("left", freecam));
    $("zoomRail").classList.toggle("hidden", !live || !opts.zoomRail);
    const locked = !!lockLook && !gapStaticActive();
    $("gapBadge").classList.toggle("hidden", !live || !(gapStaticActive() || locked));
    if (locked) { $("gapBadgeText").textContent = "LOCKED · TAP TO RE-AIM"; return; }
    let text = "STATIC";
    if (gapPrefs.dynamic) text += gapInfo && gapInfo.zoom ? ` · AUTO ${gapInfo.zoom.toFixed(2)}×` : " · AUTO";
    if (gapPrefs.track) {
      const t = gapInfo && gapInfo.track;
      text += t && t.name ? ` · ${t.name.toUpperCase()} ${t.m} m` : ` · TRACKING ≤${gapPrefs.range} m`;
    }
    $("gapBadgeText").textContent = text;
  }
  function bindGap() {
    $("swGapStatic").onclick = () => {
      gapPrefs.static = !gapPrefs.static;

      if (gapPrefs.static) { if (gapStaticActive()) gapFreeze(); else gapPrefs.look = null; }
      gapSave();
      toast(gapPrefs.static ? (preset === 6 && !freecam ? "GAP locked in place" : "Static Gap Cam on · pick GAP")
                            : "GAP follows the phone again");
    };
    $("swGapDynamic").onclick = () => {
      gapPrefs.dynamic = !gapPrefs.dynamic; gapSave();
      if (gapPrefs.dynamic && !freecam && preset === 6) setZoom(1, false);
      toast(gapPrefs.dynamic ? "Dynamic Gap Cam on · zooms out with speed" : "Dynamic Gap Cam off");
    };
    $("swGapTrack").onclick = () => {
      gapPrefs.track = !gapPrefs.track; gapSave();
      toast(gapPrefs.track ? `Player Tracking on · players within ${gapPrefs.range} m` : "Player Tracking off");
    };
    const slider = (id, key, parse) => $(id).addEventListener("input", (e) => {
      const v = parse(e.target.value);
      if (Number.isFinite(v)) { gapPrefs[key] = v; gapSave(); }
    });
    slider("gapStart", "start", (v) => parseInt(v, 10));
    slider("gapOut", "out", (v) => Math.round(parseFloat(v) * 100) / 100);
    slider("gapRange", "range", (v) => parseInt(v, 10));
    document.querySelectorAll("#gapSmooth button").forEach((b) => (b.onclick = () => { gapPrefs.smooth = b.dataset.v; gapSave(); }));

    const relock = () => {
      if (!gapStaticActive()) { toast("Pick Car View › GAP first"); return; }
      gapFreeze(); gapSave(); toast("GAP locked here");
    };
    $("gapLock").onclick = relock;
    $("gapBadge").onclick = () => {
      if (lockLook && !gapStaticActive()) { setLock(true); toast("Locked here"); return; }
      relock();
    };
    renderGap();
  }

  function sample() {
    const p = window.HLTracking.getPose();
    const frozen = gapStaticActive();
    if (frozen && !gapPrefs.look) { gapFreeze(); store.set("gap", gapPrefs); }
    return {

      euler: frozen ? (gapPrefs.look || [0, 0, 0]) : (lockLook || p.euler),
      tracking: frozen || !!lockLook || p.tracking, freecam, zoom: normalizedZoom(),
      gap_dynamic: frozen && gapPrefs.dynamic,
      gap_track: frozen && gapPrefs.track,
      gap_track_range: gapPrefs.range, gap_track_smooth: gapPrefs.smooth,
      gap_zoom_start: gapPrefs.start, gap_zoom_out: gapPrefs.out,

      position: frozen ? null : lockLook ? lockPos : scaledPosition(p),
      fov_base: opts.fovBase,
      control_sequence: ctrl.seq, control_id: ctrl.id, value: ctrl.value,
      movement: ctrl.movement,
    };
  }
  function sendSample() {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(sample()));
  }
  function emit(id, value, movement) {
    ctrl = { seq: (ctrl.seq + 1) >>> 0, id, value: value || 0, movement: movement || [0, 0, 0] };
    sendSample();
  }
  function stickMovement() {
    if (freecam) {

      const shape = (v) => Math.sign(v) * v * v * PHONE_SPEED_MAXIMUM;

      return [-shape(pad[0]), shape(lift), shape(pad[1])];
    }

    const sq = (v) => Math.sign(v) * v * v;
    return [-sq(pad[0]), sq(lift), sq(pad[1])];
  }
  function flushStick() {
    if (!stickDirty) return;
    stickDirty = false;
    const m = stickMovement();
    const moving = m.some((v) => v !== 0);
    if (moving || stickWasMoving) emit(freecam ? C.FREE_MOVE : C.NUDGE, 0, m);
    stickWasMoving = moving;
  }
  function freecamOptionBits() {
    return (opts.ignoreGround ? 1 : 0) | (opts.standUp ? 4 : 0) | (opts.drone ? 8 : 0);
  }

  function syncState() {
    emit(C.MODE, freecam ? 1 : 0);
    emit(C.PRESET, preset);
    emit(C.OPTIONS, freecamOptionBits());
    emit(C.RUN, opts.run ? 1 : 0);
    emit(C.STEADY, opts.steadycam ? 1 : 0);
    emit(C.MOTION_FX, opts.motionFx ? 1 : 0);
    emit(C.WALKING_FX, opts.walkingFx ? 1 : 0);
  }

  async function doPair() {
    const code = $("codeInput").value.trim().toUpperCase().replace(/-/g, "");
    $("pairErr").textContent = "";
    if (code.length < 4) { $("pairErr").textContent = "Enter the code from HorizonLens on your PC."; return; }
    $("pairBtn").disabled = true;
    try {
      const r = await fetch("/pair?code=" + encodeURIComponent(code)).then((x) => x.json());
      if (!r.ok) { $("pairErr").textContent = "That code doesn't match. Check HorizonLens on your PC."; return; }
      store.set("code", code);
      hlToken = r.token || "";
      await startSession();
    } catch (e) {
      $("pairErr").textContent = "Can't reach the PC. Is this phone on the same Wi-Fi?";
    } finally { $("pairBtn").disabled = false; }
  }

  async function startSession() {
    $("pair").classList.add("hidden");
    $("live").classList.remove("hidden");
    if (cfg) { $("setPcName").textContent = cfg.pcName || "This PC"; $("setHubId").textContent = cfg.hubId || "—"; $("setGame").textContent = cfg.game || "Forza Horizon 6"; }
    buildPresets();
    applyView(false);
    renderOptions();

    const ok = window.HLTracking.native ? true
      : await (window._orientP || window.HLTracking.enableOrientation());
    if (window._xrP) {
      const ar = await window._xrP;
      phoneReport(ar ? "ar-started" : "ar-failed", ar ? null : window.HLTracking.xrError);
      if (!ar) toast("Position tracking unavailable · rotation only");
    } else if (!ok) toast("Allow Motion & Orientation Access in Safari settings");
    window.HLTracking.recenter();
    reportViewport();
    sendQuality();
    window.addEventListener("resize", () => {
      clearTimeout(reportViewport._t); reportViewport._t = setTimeout(reportViewport, 600);
    });
    startWhep();
    openPoseWs();
    setInterval(() => { flushStick(); sendSample(); }, 1000 / 60);
    statsLoop();
    healthLoop();
    airPush();
    if (vistaPrefs.enabled) vistaFetch();
  }

  let recQuality = store.get("recQuality", "standard");
  function sendQuality() {
    document.querySelectorAll("#recQuality button").forEach((b) => b.classList.toggle("on", b.dataset.v === recQuality));
    fetch("/viewport", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify({ quality: recQuality }) }).catch(() => {});
  }
  function bindQuality() {
    document.querySelectorAll("#recQuality button").forEach((b) => (b.onclick = () => {
      if (b.dataset.v === recQuality) return;
      recQuality = b.dataset.v; store.set("recQuality", recQuality);
      if (!recording) expectStreamChange();
      sendQuality();
      toast(recording ? "Quality changes after this recording" : `Recording quality: ${b.textContent}`);
    }));
    document.querySelectorAll("#recQuality button").forEach((b) => b.classList.toggle("on", b.dataset.v === recQuality));
  }

  function reportViewport() {

    const portrait = window.innerHeight >= window.innerWidth;
    setZoom(zoomFactor, false);
    if (reportViewport._sent === portrait) return;
    reportViewport._sent = portrait;
    expectStreamChange();
    fetch("/viewport", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width: Math.round(window.innerWidth), height: Math.round(window.innerHeight) }),
    }).catch(() => {});
  }

  let fps = 0, rtt = 0, vidOn = false, frames = 0, framesAt = 0, recording = false, recAt = 0;
  let camOwned = null;
  function fmtTime(s) { const m = Math.floor(s / 60); return m + ":" + String(Math.floor(s % 60)).padStart(2, "0"); }

  let streamGen = null, fastHealthUntil = 0, switching = false;

  const AIR_DEFAULTS = { enabled: false, front: 0, rear: 0, drop: 60, floor: 15, speed: 2.5,
    sequence: "together", camber: false, camber_front: -6, camber_rear: -6, curve: "smooth",
    sound: true };
  const air = Object.assign({}, AIR_DEFAULTS, store.get("air", {}));
  let airState = null;
  function airSend(body) {
    return fetch("/air", { method: "POST", headers: { "Content-Type": "application/json" },
                           body: JSON.stringify(body) })
      .then((r) => r.json()).then((r) => { if (r && r.air) { airState = r.air; renderAir(); } })
      .catch(() => {});
  }
  function airSettings() { const out = Object.assign({}, air); delete out.sound; return out; }
  function airPush() {
    clearTimeout(airPush._t);
    airPush._t = setTimeout(() => airSend({ settings: airSettings() }), 150);
  }
  function airSave() { store.set("air", air); airPush(); renderAirSettings(); renderAir(); }
  const AIR_SLIDERS = [
    ["airFront", "front", "airFrontV", (v) => `${v > 0 ? "+" : ""}${Math.round(v)}%`],
    ["airRear", "rear", "airRearV", (v) => `${v > 0 ? "+" : ""}${Math.round(v)}%`],
    ["airDrop", "drop", "airDropV", (v) => `${Math.round(v)}%`],
    ["airFloor", "floor", "airFloorV", (v) => `${Math.round(v)}%`],
    ["airSpeed", "speed", "airSpeedV", (v) => `${Number(v).toFixed(1)} s`],
    ["airCamF", "camber_front", "airCamFV", (v) => `${Number(v).toFixed(1)}°`],
    ["airCamR", "camber_rear", "airCamRV", (v) => `${Number(v).toFixed(1)}°`],
  ];
  function renderAirSettings() {
    $("swAir").classList.toggle("on", air.enabled);
    $("airMore").classList.toggle("hidden", !air.enabled);
    $("swAirCamber").classList.toggle("on", air.camber);
    $("airCamberMore").classList.toggle("hidden", !air.camber);
    $("swAirSound").classList.toggle("on", air.sound);
    AIR_SLIDERS.forEach(([id, key, label, fmt]) => {
      $(id).value = air[key]; $(label).textContent = fmt(air[key]);
    });
    document.querySelectorAll("#airSeq button").forEach((b) => b.classList.toggle("on", b.dataset.v === air.sequence));
    document.querySelectorAll("#airCurve button").forEach((b) => b.classList.toggle("on", b.dataset.v === air.curve));
  }
  function renderAir() {
    const live = vidOn || camOwned === true;
    $("airRemote").classList.toggle("hidden", !air.enabled || !live);
    if (!air.enabled) return;
    const st = airState;
    let label = "--", f = "--", r = "--";
    if (st && st.sequencer) label = "SEQ";
    else if (!st || !st.enabled) label = "WAIT";
    else if (!st.car) label = "NO CAR";
    else {
      label = st.moving ? "MOVE" : st.lowered ? "DOWN" : "UP";
      if (st.front != null) { f = st.front + "%"; r = st.rear + "%"; }
    }
    $("airSt").textContent = label; $("airF").textContent = f; $("airR").textContent = r;
    $("airRemote").classList.toggle("busy", !!(st && st.moving));
    $("airUp").classList.toggle("on", !!(st && st.car && !st.lowered));
    $("airDown").classList.toggle("on", !!(st && st.car && st.lowered));
  }

  let actx = null, slamBuf = null;
  function audio() {
    if (!actx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      actx = new AC();
      fetch("/static/slam.wav").then((r) => r.arrayBuffer())
        .then((b) => actx.decodeAudioData(b)).then((buf) => { slamBuf = buf; }).catch(() => {});
    }
    if (actx.state === "suspended") actx.resume();
    return actx;
  }
  function airHiss(seconds) {
    const a = audio(); if (!a) return;
    const len = Math.max(0.2, seconds), n = Math.floor(a.sampleRate * len);
    const buf = a.createBuffer(1, n, a.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    const src = a.createBufferSource(); src.buffer = buf;
    const band = a.createBiquadFilter(); band.type = "bandpass"; band.frequency.value = 2400; band.Q.value = 0.7;
    const gain = a.createGain(), t = a.currentTime;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.35, t + 0.06);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + len);
    src.connect(band).connect(gain).connect(a.destination);
    src.start(t); src.stop(t + len);
  }
  function airSound(action) {
    if (!air.sound) return;
    try {
      const a = audio(); if (!a) return;
      if (action === "down" && slamBuf) {
        const src = a.createBufferSource(); src.buffer = slamBuf; src.connect(a.destination); src.start();
      } else {
        airHiss(Math.min(air.speed, 2.5));
      }
    } catch (e) {}
  }
  function airPress(action) {
    if (!air.enabled) return;
    if (airState && airState.car && (action === "down") === !!airState.lowered) return;
    if (navigator.vibrate) navigator.vibrate(25);
    airSound(action);
    fastHealthUntil = Date.now() + air.speed * 1000 + 1500;
    airSend({ action });
  }

  const vistaPrefs = Object.assign({ enabled: false,
    groups: { panels: true, windows: false, lights: true, car: true } }, store.get("vista", {}));
  const VISTA_GROUPS = [["panels", "Doors & panels"], ["windows", "Windows"],
                        ["lights", "Lights"], ["car", "Car"]];
  let vistaInfo = null;
  function vistaSave() { store.set("vista", vistaPrefs); renderVistaSettings(); renderVistaButton(); }
  async function vistaFetch() {
    try {
      const r = await fetch("/vista");
      const body = r.ok ? await r.json() : null;
      vistaInfo = body && Array.isArray(body.actions) ? body : null;
    } catch (e) { vistaInfo = null; }
    renderVistaSettings(); renderVistaGrid();
  }
  function vistaReadyCount() {
    return vistaInfo ? vistaInfo.actions.filter((a) => a.ok).length : 0;
  }
  function renderVistaButton() {
    const live = vidOn || camOwned === true;
    $("btnVista").classList.toggle("hidden", !vistaPrefs.enabled || !live);
  }
  function renderVistaSettings() {
    $("swVista").classList.toggle("on", vistaPrefs.enabled);
    $("vistaMore").classList.toggle("hidden", !vistaPrefs.enabled);
    document.querySelectorAll(".sw.vg").forEach((sw) => sw.classList.toggle("on", !!vistaPrefs.groups[sw.dataset.g]));
    if (!vistaInfo) { $("vistaRun").textContent = "Not reachable"; $("vistaReady").textContent = "—"; return; }
    $("vistaRun").textContent = vistaInfo.running ? "Running" : "Not running";
    $("vistaReady").textContent = `${vistaReadyCount()} of ${vistaInfo.actions.length}`;
  }
  function renderVistaGrid() {
    const status = $("vistaStatus"), grid = $("vistaGrid");
    let msg = "Checking Forzavista…", bad = false;
    if (vistaInfo) {
      if (!vistaInfo.running) { msg = "Forzavista isn't running on the PC. Start it, then tap a button."; bad = true; }
      else if (!vistaReadyCount()) { msg = "No key bindings yet: Settings › Forza Vista › Set up key bindings."; bad = true; }
      else msg = "Forzavista is ready. Keep Forza as the active window on the PC.";
    } else { msg = "Can't reach HorizonLens on the PC."; bad = true; }
    status.textContent = msg; status.classList.toggle("bad", bad);
    grid.innerHTML = "";
    const groups = VISTA_GROUPS.filter(([g]) => vistaPrefs.groups[g]);
    if (!groups.length) {
      grid.innerHTML = '<div class="vempty">Pick which buttons to show in Settings › Forza Vista.</div>';
      return;
    }
    const actions = vistaInfo ? vistaInfo.actions : [];
    groups.forEach(([g, title]) => {
      const head = document.createElement("div"); head.className = "vgroup"; head.textContent = title;
      const tiles = document.createElement("div"); tiles.className = "vtiles";
      actions.filter((a) => a.group === g).forEach((a) => {
        const t = document.createElement("button");
        t.className = "vtile press" + (a.ok ? "" : " off");
        t.innerHTML = '<span class="l"></span><span class="k"></span>';
        t.querySelector(".l").textContent = a.label;
        t.querySelector(".k").textContent = a.ok ? a.key : "not bound";
        t.onclick = () => vistaPress(a, t);
        tiles.appendChild(t);
      });
      grid.appendChild(head); grid.appendChild(tiles);
    });
  }
  async function vistaPress(a, tile) {
    if (!a.ok) { toast("Bind it in Forzavista, or use Set up key bindings"); return; }
    if (navigator.vibrate) navigator.vibrate(20);
    tile.classList.add("flash"); setTimeout(() => tile.classList.remove("flash"), 450);
    try {
      const r = await fetch("/vista", { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({ action: a.id }) }).then((x) => x.json());
      if (!r.ok) toast(r.message || "Didn't work");
    } catch (e) { toast("Can't reach the PC"); }
  }
  function bindVista() {
    $("swVista").onclick = () => {
      vistaPrefs.enabled = !vistaPrefs.enabled; vistaSave();
      if (vistaPrefs.enabled) vistaFetch();
    };
    document.querySelectorAll(".sw.vg").forEach((sw) => (sw.onclick = () => {
      vistaPrefs.groups[sw.dataset.g] = !vistaPrefs.groups[sw.dataset.g]; vistaSave(); renderVistaGrid();
    }));
    $("vistaSetup").onclick = async () => {
      try {
        const r = await fetch("/vista/setup", { method: "POST" }).then((x) => x.json());
        toast(r.message || (r.ok ? "Done" : "Didn't work"));
      } catch (e) { toast("Can't reach the PC"); }
      vistaFetch();
    };
    $("btnVista").onclick = () => { openSheet("vista"); vistaFetch(); };
    renderVistaSettings();
  }

  function bindAir() {
    $("swAir").onclick = () => {
      air.enabled = !air.enabled; airSave();
      toast(air.enabled ? "Air suspension on" : "Air suspension off · car back to stock");
    };
    $("swAirCamber").onclick = () => { air.camber = !air.camber; airSave(); };
    $("swAirSound").onclick = () => { air.sound = !air.sound; airSave(); if (air.sound) audio(); };
    AIR_SLIDERS.forEach(([id, key, label, fmt]) => {
      $(id).addEventListener("input", (e) => {
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v)) { air[key] = v; $(label).textContent = fmt(v); store.set("air", air); airPush(); }
      });
    });
    document.querySelectorAll("#airSeq button").forEach((b) => (b.onclick = () => { air.sequence = b.dataset.v; airSave(); }));
    document.querySelectorAll("#airCurve button").forEach((b) => (b.onclick = () => { air.curve = b.dataset.v; airSave(); }));
    $("airUp").onclick = () => airPress("up");
    $("airDown").onclick = () => airPress("down");
    renderAirSettings();
  }

  function expectStreamChange() { fastHealthUntil = Date.now() + 8000; }
  async function healthLoop() {
    try {
      const h = await fetch("/health").then((r) => r.json());
      camOwned = !!(h.companionLinked && h.pipelineActive);
      if (h.streamGen != null) {
        if (streamGen != null && h.streamGen !== streamGen) {
          switching = true; vidOn = false; renderStatus();
          clearTimeout(whepOnce._noFrames);
          if (pc) { try { pc.close(); } catch (e) {} }
          setTimeout(startWhep, 700);
        }
        streamGen = h.streamGen;
      }

      if (typeof h.recording === "boolean" && h.recording !== recording) {
        recording = h.recording;
        recAt = recording ? (recAt || Date.now()) : 0;
        $("btnRecord").classList.toggle("on", recording);
        renderStatus();
      }
      gapInfo = h.gap || null;
      renderGap();
      if (gapStaticActive() && (gapPrefs.dynamic || gapPrefs.track)) fastHealthUntil = Math.max(fastHealthUntil, Date.now() + 600);
      if (h.air) {
        airState = h.air;

        if (h.air.enabled !== air.enabled) airPush();
        if (h.air.moving) fastHealthUntil = Math.max(fastHealthUntil, Date.now() + 1000);
      }
    } catch (e) { camOwned = null; }
    setTimeout(healthLoop, Date.now() < fastHealthUntil ? 300 : 2000);
  }
  function renderStatus() {
    const st = $("status");
    const motion = window.HLTracking.isLive();
    st.classList.toggle("rec", recording);
    st.querySelector(".led").classList.toggle("off", !(ws && ws.readyState === 1) || !motion);
    let text = "Waiting for Forza";
    if (vidOn) text = camOwned === false ? "Connecting to Forza…" : `${fps} FPS · ${rtt} ms`;
    else if (switching) text = "Switching view…";
    else if (camOwned) text = "Starting Live View…";
    if (!motion && ws && ws.readyState === 1) text = "Motion access off";
    else if (window.HLTracking.xrActive && !window.HLTracking.positionOk && vidOn)
      text = "AR tracking limited · point at the room";
    if (recording) text = "REC " + fmtTime((Date.now() - recAt) / 1000) + (vidOn ? ` · ${fps} FPS` : "");
    $("statusText").textContent = text;
    $("setVideo").textContent = vidOn ? `${fps} FPS · ${rtt} ms` : "Waiting";
    const connected = ws && ws.readyState === 1;
    $("pcDot").classList.toggle("off", !connected);
    $("pcState").textContent = connected ? "PC Receiving Camera Data" : "Not Connected";
    $("waiting").classList.toggle("hidden", vidOn || camOwned === true);
    applyView(false);
    renderAir();
    renderVistaButton();
    renderGap();
  }
  async function statsLoop() {
    if (pc && vidOn) {
      try {
        const stats = await pc.getStats();
        stats.forEach((s) => {
          if (s.type === "inbound-rtp" && s.kind === "video") {
            const now = performance.now();
            if (framesAt && now - framesAt > 400) {
              fps = Math.max(0, Math.round((s.framesDecoded - frames) / ((now - framesAt) / 1000)));
              frames = s.framesDecoded; framesAt = now;
            } else if (!framesAt) { frames = s.framesDecoded; framesAt = now; }
          }
          if (s.type === "candidate-pair" && s.state === "succeeded" && typeof s.currentRoundTripTime === "number")
            rtt = Math.round(s.currentRoundTripTime * 1000);
        });
      } catch (e) {}
    }
    renderStatus();
    setTimeout(statsLoop, 500);
  }

  let whepBusy = false;
  let reportedAt = 0;

  async function phoneReport(event, error) {
    const info = { event, error: error ? String(error).slice(0, 200) : undefined,
                   video: { w: $("video").videoWidth, h: $("video").videoHeight,
                            ready: $("video").readyState } };
    try {
      if (pc) {
        info.ice = pc.iceConnectionState; info.conn = pc.connectionState;
        const stats = await pc.getStats(), codecs = {};
        stats.forEach((s) => { if (s.type === "codec") codecs[s.id] = s; });
        stats.forEach((s) => {
          if (s.type === "inbound-rtp" && s.kind === "video") {
            const c = codecs[s.codecId] || {};
            Object.assign(info, { codec: c.mimeType, fmtp: c.sdpFmtpLine,
              packets: s.packetsReceived, frames: s.framesReceived,
              decoded: s.framesDecoded, keyframes: s.keyFramesDecoded,
              dropped: s.framesDropped, decoder: s.decoderImplementation,
              size: s.frameWidth ? `${s.frameWidth}x${s.frameHeight}` : undefined });
          }
        });
      }
    } catch (e) {}
    try {
      await fetch("/phonelog", { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(info) });
    } catch (e) {}
  }
  async function startWhep() {
    if (whepBusy) return;
    whepBusy = true;
    try {
      for (;;) {
        try { await whepOnce(); return; } catch (e) {
          console.warn("WHEP", e);
          if (Date.now() - reportedAt > 30000) { reportedAt = Date.now(); phoneReport("whep-error", e); }
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
    } finally { whepBusy = false; }
  }
  function iceGathered(p) {
    if (p.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((res) => {
      const to = setTimeout(res, 1500);
      p.onicegatheringstatechange = () => { if (p.iceGatheringState === "complete") { clearTimeout(to); res(); } };
    });
  }
  async function whepOnce() {
    if (pc) { try { pc.close(); } catch (e) {} }
    pc = new RTCPeerConnection({ iceServers: [] });
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (e) => {
      const v = $("video");

      clearTimeout(whepOnce._noFrames);
      whepOnce._noFrames = setTimeout(async () => {
        if (vidOn) return;
        await phoneReport("no-picture");
        fetch("/livefail", { method: "POST" }).catch(() => {});
      }, 8000);
      v.onloadeddata = () => {
        clearTimeout(whepOnce._noFrames);
        vidOn = true; switching = false; framesAt = 0; renderStatus();
        setTimeout(() => phoneReport("playing"), 2000);
      };
      if (v.srcObject !== e.streams[0]) { v.srcObject = e.streams[0]; v.play().catch(() => {}); }
    };
    const mine = pc;
    pc.onconnectionstatechange = () => {

      if (mine !== pc || mine._retrying) return;
      if (["failed", "disconnected", "closed"].includes(mine.connectionState)) {
        mine._retrying = true;
        vidOn = false; renderStatus();
        setTimeout(startWhep, 1000);
      }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await iceGathered(pc);
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 6000);
    let res;
    try {
      res = await fetch(cfg.whepUrl, { method: "POST", headers: { "Content-Type": "application/sdp" },
                                       body: pc.localDescription.sdp, signal: ctl.signal });
    } finally { clearTimeout(to); }
    if (!res.ok) throw new Error("WHEP " + res.status);
    await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
  }

  function openPoseWs() {
    ws = new WebSocket(cfg.poseWs + "?t=" + encodeURIComponent(hlToken));
    ws.onopen = () => { syncState(); renderStatus(); };
    ws.onclose = () => { renderStatus(); setTimeout(openPoseWs, 1500); };
  }

  function applyView(changed) {

    const live = vidOn || camOwned === true;
    $("tabCar").classList.toggle("active", !freecam);
    $("tabFree").classList.toggle("active", freecam);
    $("carControls").classList.toggle("hidden", !live || freecam || !adjust);
    const rowWasHidden = $("carRow").classList.contains("hidden");
    $("carRow").classList.toggle("hidden", !live || freecam);

    if (rowWasHidden && live && !freecam) requestAnimationFrame(centerPreset);
    $("freeControls").classList.toggle("hidden", !live || !freecam);
    $("runBadge").classList.toggle("hidden", !live || !freecam || !opts.run);
    $("btnAdjust").classList.toggle("off", !adjust);
    if (typeof renderLock === "function") renderLock();
    if (changed) {
      resetSticks();
      store.set("freecam", freecam);
      emit(C.MODE, freecam ? 1 : 0);
      if (freecam) setZoom(1, false); else applyPresetZoom(false);
      window.HLTracking.recenter();
      requestAnimationFrame(centerPreset);
    }
  }
  function setFreecam(on) { if (on !== freecam) { freecam = on; applyView(true); renderLock(); } }

  function buildPresets() {
    const track = $("presetTrack"); track.innerHTML = "";
    PRESETS.forEach((p) => {
      const b = document.createElement("button");
      b.className = "preset"; b.textContent = p.name; b.dataset.id = p.id;
      b.onclick = () => selectPreset(p.id);
      track.appendChild(b);
    });

    let sx = null;
    $("presetRow").addEventListener("touchstart", (e) => { sx = e.touches[0].clientX; }, { passive: true });
    $("presetRow").addEventListener("touchend", (e) => {
      if (sx === null) return;
      const dx = e.changedTouches[0].clientX - sx; sx = null;
      if (Math.abs(dx) < 40) return;
      const i = PRESETS.findIndex((p) => p.id === preset);
      const j = Math.max(0, Math.min(PRESETS.length - 1, i + (dx < 0 ? 1 : -1)));
      if (j !== i) selectPreset(PRESETS[j].id);
    });
    window.addEventListener("resize", centerPreset);
    markPreset();
  }
  function markPreset() {
    [...$("presetTrack").children].forEach((b) => b.classList.toggle("active", +b.dataset.id === preset));
    $("saveName").textContent = presetName(preset);
    requestAnimationFrame(centerPreset);
  }
  function centerPreset() {
    const active = $("presetTrack").querySelector(".preset.active");
    if (!active || !active.offsetWidth) return;

    const center = window.innerWidth / 2;
    $("presetTrack").style.transform =
      `translateX(${center - (active.offsetLeft + active.offsetWidth / 2)}px)`;
  }
  function selectPreset(id) {
    if (id === preset) return;
    zoomFor[preset] = zoomFactor;
    preset = id; store.set("preset", id);
    resetSticks();
    emit(C.PRESET, id);
    markPreset();
    applyPresetZoom();
    renderGap();
  }
  const zoomFor = store.get("zoomFor", {});
  function applyPresetZoom(show = true) {
    if (freecam) return;
    setZoom(zoomFor[preset] ?? PRESET_DEFAULT_ZOOM[preset] ?? 1, show);
  }

  function resetSticks() {
    pad[0] = 0; pad[1] = 0; lift = 0;
    document.querySelectorAll(".pad .knob").forEach((k) => { k.style.transform = "translate(-50%,-50%)"; });
    document.querySelectorAll(".height .knob").forEach((k) => { k.style.top = "50%"; });
    stickDirty = true; flushStick();
  }
  function bindPad(el) {
    const knob = el.querySelector(".knob");
    let id = null, cx = 0, cy = 0;
    const R = () => el.clientWidth * 0.31;
    const set = (x, y) => {
      let dx = x - cx, dy = y - cy;
      const r = R(), d = Math.hypot(dx, dy);
      if (d > r) { dx *= r / d; dy *= r / d; }
      knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
      pad[0] = dx / r; pad[1] = -dy / r; stickDirty = true;
    };
    el.addEventListener("pointerdown", (e) => {
      id = e.pointerId; el.setPointerCapture(id); el.classList.add("drag");
      const b = el.getBoundingClientRect(); cx = b.left + b.width / 2; cy = b.top + b.height / 2;
      set(e.clientX, e.clientY);
    });
    el.addEventListener("pointermove", (e) => { if (e.pointerId === id) set(e.clientX, e.clientY); });
    const end = (e) => {
      if (e.pointerId !== id) return;
      id = null; el.classList.remove("drag");
      knob.style.transform = "translate(-50%,-50%)";
      pad[0] = 0; pad[1] = 0; stickDirty = true; flushStick();
    };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  }
  function bindHeight(el) {
    const track = el.querySelector(".track"), knob = el.querySelector(".knob");
    let id = null;
    const set = (y) => {
      const b = track.getBoundingClientRect();
      const travel = (b.height - knob.offsetHeight) / 2;
      const dy = Math.max(-travel, Math.min(travel, y - (b.top + b.height / 2)));
      knob.style.top = `calc(50% + ${dy}px)`;
      lift = -dy / travel; stickDirty = true;
    };
    el.addEventListener("pointerdown", (e) => {
      id = e.pointerId; el.setPointerCapture(id); el.classList.add("drag"); set(e.clientY);
    });
    el.addEventListener("pointermove", (e) => { if (e.pointerId === id) set(e.clientY); });
    const end = (e) => {
      if (e.pointerId !== id) return;
      id = null; el.classList.remove("drag"); knob.style.top = "50%";
      lift = 0; stickDirty = true; flushStick();
    };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  }

  function bindPinch() {
    const touches = new Map();
    let base = null;
    const v = $("live");
    const spread = () => { const [a, b] = [...touches.values()]; return Math.hypot(a.x - b.x, a.y - b.y); };
    v.addEventListener("pointerdown", (e) => {
      if (e.target !== $("video") && e.target !== $("waiting") && e.target !== v) return;
      touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (touches.size === 2) base = { d: spread(), z: zoomFactor };
    });
    v.addEventListener("pointermove", (e) => {
      if (!touches.has(e.pointerId)) return;
      touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (touches.size === 2 && base) setZoom(base.z * spread() / base.d, true);
    });
    const end = (e) => { touches.delete(e.pointerId); if (touches.size < 2) base = null; };
    v.addEventListener("pointerup", end); v.addEventListener("pointercancel", end);

    let last = 0;
    $("video").addEventListener("click", () => {
      const now = Date.now(); if (now - last < 300) setZoom(1, true); last = now;
    });
  }

  function openSheet(id) { $(id).classList.add("show"); }
  function closeSheets() { document.querySelectorAll(".scrim.show").forEach((s) => s.classList.remove("show")); }

  let xrOk = false;
  function renderTracking() {

    const ar = xrOk || window.HLTracking.native;
    $("arGroup").classList.toggle("hidden", !ar);
    $("arSec").classList.toggle("hidden", !ar);
    $("arRow").classList.toggle("hidden", !xrOk);
    $("swAR").classList.toggle("on", window.HLTracking.xrActive);
    $("setTracking").textContent = window.HLTracking.xrActive ? "Position + rotation (AR)" : "Rotation only";
  }
  async function toggleAR() {
    if (window.HLTracking.xrActive) {
      opts.ar = false; store.set("opts", opts);
      await window.HLTracking.stopXR();
    } else {
      opts.ar = true; store.set("opts", opts);
      const ok = await window.HLTracking.startXR(document.body);
      phoneReport(ok ? "ar-started" : "ar-failed", ok ? null : window.HLTracking.xrError);
      if (!ok) toast("AR unavailable: " + (window.HLTracking.xrError || "unknown"));
    }
    renderTracking();
  }
  window.HLTracking.onXRChange = (on) => {

    window.HLTracking.recenter();
    emit(C.RECENTER, 0);
    renderTracking();
    if (!on) toast(window.HLTracking.native ? "Tracking paused"
                                            : "Position tracking stopped · turn it on in ⋯");
  };

  function renderOptions() {
    document.querySelectorAll("#options .sw").forEach((s) => s.classList.toggle("on", !!opts[s.dataset.opt]));
    $("runBadge").classList.toggle("hidden", !vidOn || !freecam || !opts.run);
  }
  function setOption(key, on) {
    opts[key] = on; store.set("opts", opts); renderOptions();
    if (key === "run") emit(C.RUN, on ? 1 : 0);
    else if (key === "steadycam") emit(C.STEADY, on ? 1 : 0);
    else if (key === "motionFx") emit(C.MOTION_FX, on ? 1 : 0);
    else if (key === "walkingFx") emit(C.WALKING_FX, on ? 1 : 0);
    else if (key === "zoomRail") renderLock();
    else emit(C.OPTIONS, freecamOptionBits());
  }
  async function loadRecordings() {
    const list = $("recList");
    list.innerHTML = '<div class="empty">Loading…</div>';
    try {
      const r = await fetch("/recordings").then((x) => x.json());
      if (!r.recordings || !r.recordings.length) {
        list.innerHTML = '<div class="empty">No recordings yet. Tap the red button to record.</div>';
        return;
      }
      list.innerHTML = "";
      r.recordings.forEach((rec) => {
        const a = document.createElement("a");
        a.className = "rec";
        a.href = "/recordings/" + encodeURIComponent(rec.name) + "?t=" + encodeURIComponent(hlToken);
        a.setAttribute("download", rec.name);
        const when = new Date(rec.modified * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
        a.innerHTML = `<span class="n"></span><span class="m">${rec.sizeMb} MB</span>`;
        a.querySelector(".n").textContent = when;
        list.appendChild(a);
      });
    } catch (e) { list.innerHTML = '<div class="empty">Couldn\'t reach the PC.</div>'; }
  }

  window.addEventListener("DOMContentLoaded", async () => {
    try { cfg = await fetch("/config.json").then((r) => r.json()); } catch (e) {}
    document.body.classList.toggle("plain", !store.get("glass", true));
    $("swGlass").classList.toggle("on", store.get("glass", true));

    const askOrient = () => {
      if (!window._orientP && !window.HLTracking.native)
        window._orientP = window.HLTracking.enableOrientation().then((ok) => { if (!ok) window._orientP = null; return ok; });
    };
    $("pairBtn").addEventListener("pointerdown", askOrient);
    $("pairBtn").addEventListener("click", askOrient, true);

    $("pairBtn").addEventListener("click", () => {
      if (xrOk && opts.ar !== false && !window._xrP)
        window._xrP = window.HLTracking.startXR(document.body);
    }, true);
    window.HLTracking.xrSupported().then((ok) => { xrOk = ok; renderTracking(); });
    $("swAR").onclick = toggleAR;
    $("pairBtn").onclick = doPair;
    $("codeInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doPair(); });
    const urlCode = new URLSearchParams(location.search).get("code") || store.get("code", "");
    if (new URLSearchParams(location.search).get("expired"))
      $("pairErr").textContent = "HorizonLens restarted on the PC. Scan its new QR code to reconnect.";
    if (urlCode) { $("codeInput").value = urlCode.toUpperCase(); $("pairBtn").textContent = "Tap to Start"; }

    $("tabCar").onclick = () => setFreecam(false);
    $("tabFree").onclick = () => setFreecam(true);
    $("btnAdjust").onclick = () => {
      adjust = !adjust; store.set("adjust", adjust);
      if (!adjust) resetSticks();
      applyView(false);
    };
    $("btnSave").onclick = () => {
      resetSticks();
      emit(C.SAVE, preset);
      const b = $("btnSave"); b.classList.add("flash"); setTimeout(() => b.classList.remove("flash"), 500);
      toast("Saved " + presetName(preset).charAt(0) + presetName(preset).slice(1).toLowerCase());
    };
    $("btnRecenter").onclick = () => {
      window.HLTracking.recenter();
      resetSticks();
      emit(C.RECENTER, 0);
      if (gapStaticActive()) {

        gapPrefs.look = [(gapPrefs.look || [0])[0] || 0, 0, 0]; gapSave();
        toast("Static GAP pointed straight ahead");
        return;
      }
      if (lockLook) {
        lockLook = [lockLook[0] || 0, 0, 0]; renderLock();
        toast("Locked · pointed straight ahead");
        return;
      }
      toast(freecam ? "Recentered" : "Recentered · unsaved framing reset");
    };
    $("runBadge").onclick = () => { setOption("run", false); toast("Run off"); };

    let recBusy = false;
    $("btnRecord").onclick = async () => {
      if (recBusy) return; recBusy = true;
      try {
        const r = await fetch("/record", { method: "POST" }).then((x) => x.json());
        if (!r.ok) { toast("Recording unavailable"); return; }
        recording = !!r.recording; recAt = Date.now();
        $("btnRecord").classList.toggle("on", recording);
        toast(recording ? "Recording on the PC" : "Saved to Recordings on the PC");
        renderStatus();
      } catch (e) { toast("Recording unavailable"); }
      finally { recBusy = false; }
    };

    $("btnSettings").onclick = () => { openSheet("settings"); if (vistaPrefs.enabled) vistaFetch(); };
    $("btnGrid").onclick = () => openSheet("options");
    $("btnGallery").onclick = () => { openSheet("gallery"); loadRecordings(); };
    document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = closeSheets));
    document.querySelectorAll(".scrim").forEach((s) =>
      s.addEventListener("click", (e) => { if (e.target === s) closeSheets(); }));
    document.querySelectorAll("#options .sw").forEach((s) =>
      (s.onclick = () => setOption(s.dataset.opt, !opts[s.dataset.opt])));
    $("optZoom").addEventListener("input", (e) => setZoom(parseFloat(e.target.value), false));
    const renderSliders = () => {
      $("optFov").value = opts.fovBase; $("optFovV").textContent = Math.round(opts.fovBase) + "°";
      $("optMove").value = opts.moveScale; $("optMoveV").textContent = Number(opts.moveScale).toFixed(2) + "×";
    };
    $("optFov").addEventListener("input", (e) => {
      opts.fovBase = Math.max(50, Math.min(100, parseFloat(e.target.value) || 70));
      store.set("opts", opts); renderSliders();
    });
    $("optMove").addEventListener("input", (e) => {
      opts.moveScale = Math.max(0.25, Math.min(5, parseFloat(e.target.value) || 1.5));
      store.set("opts", opts); renderSliders();
    });
    renderSliders();
    $("swGlass").onclick = () => {
      const on = !$("swGlass").classList.contains("on");
      $("swGlass").classList.toggle("on", on); store.set("glass", on);
      document.body.classList.toggle("plain", !on);
    };

    const toApp = (cmd) => window.webkit.messageHandlers.horizonlens.postMessage({ cmd });
    $("setPairAgain").onclick = () => {
      store.set("code", "");
      if (window.HLTracking.native) toApp("scan"); else location.href = location.pathname;
    };
    $("setForget").onclick = () => {
      if (!confirm("Forget this PC? You'll need to pair again from HorizonLens.")) return;
      try { Object.keys(localStorage).filter((k) => k.startsWith("hl_")).forEach((k) => localStorage.removeItem(k)); } catch (e) {}
      if (window.HLTracking.native) toApp("forget"); else location.href = location.pathname;
    };

    bindPad($("padFree")); bindPad($("padCar"));
    bindHeight($("heightFree")); bindHeight($("heightCar"));
    bindPinch();
    bindAir();
    bindVista();
    bindGap();
    bindZoomRail();
    bindQuality();

    const renderHide = () => {
      const on = store.get("hideBtn", false);
      $("swHideBtn").classList.toggle("on", on);
      $("btnHide").classList.toggle("hidden", !on);
      if (!on) $("live").classList.remove("clean");
    };
    $("swHideBtn").onclick = () => {
      store.set("hideBtn", !store.get("hideBtn", false)); renderHide();
      toast(store.get("hideBtn", false) ? "Hide button added (top left)" : "Hide button removed");
    };
    $("btnHide").onclick = () => {
      const clean = $("live").classList.toggle("clean");
      $("btnHide").setAttribute("aria-label", clean ? "Show controls" : "Hide controls");
      if (clean) { resetSticks(); }
    };
    renderHide();
    $("btnLock").onclick = () => {
      setLock(!lockLook);
      toast(lockLook ? "Locked in place · only the stick and HEIGHT move it" : "Unlocked · follows the phone");
    };
    if (freecam) setZoom(1, false); else applyPresetZoom(false);
  });
})();
