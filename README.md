# HorizonLens

Use your phone as a hand-held camera inside **Forza Horizon 6** (Steam and
Microsoft Store), with the game streamed live to the phone and recordings
saved on the PC.

Point the phone and the in-game camera points with it. Walk, lean and crouch
with an AR-capable phone and the camera moves with you. Record the shot on
the PC with game sound.

## Features

- **Car View** presets: Passenger, Driver, Window, Bumper, Wheel and GAP (a
  fishbowl camera behind the car), each movable with the stick and height
  control and saved per preset.
- **Freecam**: walk around the car with the stick, height, Run, Steadycam and
  Drone Inertia.
- **Position tracking**: Android Chrome with ARCore (WebXR), and the iPhone
  app (ARKit). Other phones track rotation.
- **Gap Cam**: Static Gap Cam (put the phone down and drive), Dynamic Gap Cam
  (zooms out smoothly with speed) and Player Tracking (follows other players
  driving near you; AI traffic and parked cars are ignored).
- **Lock** for dolly and slider shots, an on-screen **zoom slider**, and a
  **Hide** button that leaves only the picture on the phone.
- **Recording** on the PC with game sound: Standard (the live view), High
  (1080x1920) or Max (the game's full resolution) through a dedicated capture.
- **Cinematic sequencer** inside the game: keyframed shots on a timeline,
  zoom, air suspension and Forza Vista animation tracks.
- **Air suspension** and a **Forza Vista remote** (doors, hood, lights...)
  from the phone.

## Download and use

Download `HorizonLens-1.0.0-win64.zip` from the
[Releases](../../releases) page, unzip it anywhere and follow `README.txt`
inside. In short:

1. Run `Allow-Firewall.bat` once (your Wi-Fi must be set to Private).
2. Start Forza Horizon 6 and get into free roam.
3. Run `HorizonLens.exe`, press **Start** and scan the QR code with your phone
   (same Wi-Fi as the PC).

Requirements: Windows 10 or 11 (64-bit), Forza Horizon 6 (Steam or Microsoft
Store), and a phone on the same Wi-Fi: iPhone (Safari or the HorizonLens app)
or Android (Chrome). NVIDIA GPUs capture the game straight from its frames;
AMD and Intel use window capture.

Problems? Run `Self-Test.bat` with HorizonLens open and share the report on
the Discord: https://discord.gg/fh6

## How it works

| Part | Where | What it does |
|---|---|---|
| PC app | `horizonlens/` (Python, PySide6) | Finds the game, loads the camera module, captures and streams the game, runs the phone's web server, records, hosts the sequencer overlay. |
| Phone page | `web/` | Served by the PC over HTTPS on the LAN: controls, live view (WebRTC via MediaMTX) and tracking. |
| iPhone app | `ios/` | The same page in a web view plus ARKit position tracking. Built by GitHub Actions (`.github/workflows/ios.yml`), sideloaded. |
| Camera module | `vendor/companion/` | RealCam's camera module for Forza Horizon 6 (see Credits). |
| Capture helper | `vendor/capture/` | RealCam's NVIDIA game capture helper (see Credits). |

## Build from source

```
py -3.12 -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File packaging\fetch-runtimes.ps1
py -3.12 run.py
```

`fetch-runtimes.ps1` downloads FFmpeg and MediaMTX into `vendor\` (too large
for git).

Tests:

```
py -3.12 -m unittest discover -s tests
```

Windows app:

```
py -3.12 -m pip install pyinstaller
py -3.12 -m PyInstaller HorizonLens.spec --noconfirm --clean --distpath build/out
```

Output: `build/out/HorizonLens/`. For a release zip add `USER_GUIDE.txt` (as
`README.txt`), `LICENSE` (as `LICENSE.txt`) and the files in `packaging/`
next to `HorizonLens.exe`, and the iPhone app in an `iPhone` folder.

## Credits

- **RealCam**: HorizonLens's camera control and NVIDIA game capture are built
  on RealCam's Forza Horizon 6 camera module (`HorizonLensCamera.dll`, RealCam's
  `RealCamForza6.dll`) and capture helper (`HorizonLensCapture.exe`, RealCam's
  `RealCamForzaNvencCapture.exe`). These are third-party files by the RealCam
  team, included unmodified apart from their names. All credit for them goes
  to RealCam; they are not covered by this project's license.
- **Forzavista** by HTTPJXRDN: the Forza Vista remote presses its key bindings.
- **FFmpeg**, **MediaMTX**, **Qt / PySide6**, **aiohttp** and the other libraries
  listed in [THIRD-PARTY-NOTICES.txt](packaging/THIRD-PARTY-NOTICES.txt).

## License

HorizonLens is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License version 3 as published by the Free
Software Foundation. See [LICENSE](LICENSE).

The third-party files listed in [THIRD-PARTY-NOTICES.txt](packaging/THIRD-PARTY-NOTICES.txt),
including RealCam's camera module and capture helper, keep their own licenses
and are not covered by the GPL.

## Disclaimer

HorizonLens is a fan-made tool. It is not made, endorsed or supported by
Microsoft, Xbox Game Studios or Playground Games. Forza Horizon is a trademark
of Microsoft. HorizonLens loads a camera module into the game process to move
the camera: use it at your own risk.
