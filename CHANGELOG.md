# Changelog

## 1.0.0 (first official release)

Everything from the 0.3.0 betas, plus:

### New
- Gap Cam: Static Gap Cam, Dynamic Gap Cam (adjustable start speed and zoom
  out) and Player Tracking (adjustable range and smoothness).
- Lock (6DOF) for dolly and slider shots in every view.
- On-screen zoom slider and a Hide button for a clean phone screen.
- Recording quality: High (1080x1920) and Max (the game's full resolution)
  through a dedicated capture, without touching the live view.
- Realistic Walking FX (human walking pace, gentle bob and sway).

### Reliability
- The phone page's server can no longer stop answering ("Can't reach
  HorizonLens"): it uses the selector event loop, a watchdog reopens it if it
  ever stops accepting, and problems are written to `server.log`.
- If the phone page can't start (its port is taken), the app says so instead
  of showing a QR code that leads nowhere.
- The QR code follows the PC's current address (Wi-Fi reconnects, new IP).
- Taking the camera waits for a driving view, so trying too early (menus,
  loading) no longer leaves camera control off until Forza restarts.
- The media server restarts on its own if it stops during a session.
- The live view follows a game resolution change (ultrawide and others).
- Recordings finish cleanly when the sound device is missing or unplugged.
- Steam installs on any drive are found (from the registry).
- A closed game's old session no longer shows camera control as ready.
- Odd values from the phone are ignored instead of dropping its connection.
