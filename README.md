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
  `docs/first-light-2026-11-15.jpg`. It shows the basement stairwell in
  near-darkness — the room lights were off.

Lessons, learned the honest way:

1. **Exposure warm-up is not optional.** Skipping 30 frames (~1.5 s) gave a
   near-black frame with only LEDs visible. Skipping 200 frames (~10 s)
   let the auto-exposure settle and the scene (stairs, block wall) became
   readable after brightening. `snap.sh` defaults to 200.
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
