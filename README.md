# Skysong

Tooling for the Bickford Llama Ranch pasture surveillance system —
prototyped indoors, before any camera posts are bought or built.

The full system design (7 solar posts, Reolink PoE cameras, point-to-point
WiFi backhaul, Frigate NVR, no cloud) lives in the ranch's planning records.
This repo holds the working tools, in the order they were proven.

## Prototype 0 — first light (2026-11-15)

Goal: prove the find → capture → retrieve path end-to-end with hardware
already on hand.

- `tools/fleet-probe.sh` swept 18 online tailnet nodes for `/dev/video*`.
  Exactly one camera exists on the fleet: a **Logitech BRIO** (USB, 4K)
  attached to **ni-knight**, a box in the basement bedroom.
- `tools/snap.sh` captured a 3840×2160 still from it; the frame is at
  `docs/first-light-2026-11-15.jpg`. The room lights were off; the frame
  is near-black, and the points of light in it are the status LEDs of the
  other ranch nodes that share the room. (An early reading of the
  brightened frame claimed stairs and a block wall — there are no stairs
  in that room. That was pattern-matching on noise, corrected by Will
  2026-11-15.)

Lessons, learned the honest way:

1. **Exposure warm-up is not optional.** Skipping 30 frames (~1.5 s) gave a
   near-black frame with only LEDs visible. Skipping 200 frames (~10 s)
   let the auto-exposure settle; the node lights became clean points
   instead of smears. `snap.sh` defaults to 200.
2. **A webcam is not a pasture camera.** The BRIO has no infrared and no
   spotlight; in a dark room it is nearly blind. Fine for prototyping the
   pipeline — the pasture design's cameras carry their own IR for this
   exact reason.
3. **Permissions:** `wbic16` is not in the `video` group on ni-knight;
   capture ran via sudo. A real install should add the service user to
   `video` instead.

## Tools

| Tool | What it does |
|---|---|
| `tools/find-cameras.sh` | List V4L2 cameras attached to the local machine, with names and by-id paths |
| `tools/fleet-probe.sh` | Probe tailnet nodes over SSH and report which have cameras attached |
| `tools/snap.sh` | Capture one still from a V4L2 device, with exposure warm-up |

## Next steps

- Stream prototype: serve the BRIO as an RTSP source (go2rtc/mediamtx) and
  view it from another machine — the shape the real posts will use.
- Motion/event capture on the stills path (trail-cam pattern).
- When the first Reolink post is built, this repo gets its post-agent.

## The camera service (2026-11-15)

Every camera host runs the same agent: `agent/skysong-agent.py`
(Python stdlib only; needs `fswebcam`, `ffmpeg`, `v4l2-ctl` on the host).

- **Port: 8642** — the same on every camera host. The agent binds
  127.0.0.1 and the node's tailnet IP only (never the LAN/Wi-Fi side),
  and each node also serves it over HTTPS via Tailscale Serve
  (e.g. https://ni-knight.pizzly-halfmoon.ts.net/).
- `GET /` — this camera's portal: view, PTZ sliders, effect sliders,
  snapshot + MP4 buttons.
- `GET /grid` — the grid portal: one tile per registered camera
  (`agent/cameras.json`), snapshots refreshing every 15 s, each tile
  linking to that camera's own portal. Every agent serves the grid;
  adding a camera = install the agent on its host + add one registry line.
- `GET /snapshot.jpg` — one still, with optional effect params
  (`b` brightness, `c` contrast, `g` gamma, `sat`, `il`/`ih` input levels).
- `GET /clip.mp4?seconds=N` — an N-second MP4 (1080p), same effect params.
- `GET /api/controls`, `POST /api/ptz` — the camera's UVC controls.
  The BRIO exposes pan/tilt (±36°) and zoom (100–500 = 1–5×) natively.
  Known wrinkle: a combined pan+tilt+zoom set saw pan read back as 0
  while tilt and zoom held — under investigation, service-side sets are
  accepted.
- `POST /api/sensor` — sensor-level brightness/contrast/saturation/gain/
  sharpness, distinct from the per-request effects above.

Install on a camera host: `deploy/install-agent.sh` (systemd unit
included; the service user joins the `video` group).

## Agent v2 — cache, novelty, UI (2026-11-15)

- Background capture loop (15 s) keeps a cached latest frame on disk;
  /snapshot.jpg serves the cache instantly (~55 ms vs ~10 s live);
  ?fresh=1 forces a live capture.
- Novelty first: each frame gets a 32x18 grayscale signature scored
  against a persisted baseline (EMA of non-novel frames). A frame at
  or above the threshold (10/255) is novel and becomes the reference
  image at /reference.jpg — always the most recent non-baseline frame.
  /api/status reports ages, scores, and recent novel events.
- Portal UI rebuilt: reference image and live cached view side by
  side, status pills, auto-refresh; grid tiles show per-camera status.
- Verified live on ni-knight: baseline noise score ~3-4 in the dark
  room; a sensor gain/brightness stimulus scored 67.6 and promoted
  the reference. In near-darkness pixel novelty is weak by physics —
  the decisive test is a light change.
