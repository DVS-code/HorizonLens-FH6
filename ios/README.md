# HorizonLens for iPhone

A small app that adds ARKit position + rotation tracking (6DOF) to the
HorizonLens phone page. The page itself comes from HorizonLens on your PC;
the app shows it and feeds it the ARKit camera pose 60 times a second.

## Build

Every push builds an unsigned `.ipa` on GitHub Actions (no Mac needed):
Actions → latest run → Artifacts → **HorizonLens-iPhone**.

## Install (sideloading, free Apple ID)

1. Install [Sideloadly](https://sideloadly.io) on your PC and plug in the iPhone.
2. Drag `HorizonLens-unsigned.ipa` into Sideloadly, enter your Apple ID, Start.
3. On the iPhone: Settings → General → VPN & Device Management → trust your
   Apple ID. On iOS 16+, also turn on Settings → Privacy & Security →
   Developer Mode (the iPhone restarts).
4. A free Apple ID signs apps for 7 days: re-sign with Sideloadly each week.

## Use

Start HorizonLens on the PC, press Start, open the app and scan the QR code.
Allow the camera (ARKit tracking only) and local network access.
