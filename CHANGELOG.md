# Changelog

## 1.2.0

### Forza Horizon 6 Series 7
- Supports the Series 7 update (Steam 2.479.874, Microsoft Store 3.479.874)
  with RealCam 1.1's camera modules and capture helper; the previous Steam
  build (2.461.691) keeps working.
- The FOV fix uses the running game build's own addresses, or changes
  nothing for a build it doesn't know.
- A Forza update HorizonLens doesn't know yet now says so plainly, with the
  build numbers and where to get the update.

### New, all optional in the settings
- Floating joystick (... > Controls > Joystick).
- Touch look: drag the picture to turn the camera, on top of the phone's
  motion or instead of it, with speed and invert (... > Controls > Look).
- Grid lines for framing (... > Lens).
- Hold-to-record (Settings > Recording > Record Button).
- Smooth Video against Wi-Fi stutter (Settings > Live View).
- Restart live view, on the phone (Settings > Live View) and in the PC window.

### Fixes
- Android: motion that stops arriving is picked up again on its own; tap the
  "Motion off" pill for help and to ask for access again.
- The phone and the PC window show when the camera module in the game stops
  answering ("Camera control paused" / "not responding").
- The HTTPS certificate repairs itself if it is damaged, falls back to
  another folder if security software blocks the usual one, and the PC
  window says when phones can't connect because of it.
- Self-test: the firewall check no longer crashes and tells you if the rule
  belongs to another HorizonLens folder; new Network type check (Public
  networks block phones); Forza's FOV settings and any lowering are listed.
- The pairing page explains a wrong code (it changes every Start) and warns
  when it is opened on a PC instead of the phone.
- After a minute with no phone, the PC window lists the usual causes
  (network type, firewall, Android's Nearby devices permission).
- New troubleshooting and fullscreen sections in the user guide.

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
