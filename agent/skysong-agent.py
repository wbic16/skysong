#!/usr/bin/env python3
"""skysong-agent - one agent per camera host. Same interface on every camera.

Endpoints (default port 8642, bound to 127.0.0.1 + the node's tailnet IP):
  GET  /                     this camera's portal page
  GET  /grid                 grid portal of all registered cameras
  GET  /snapshot.jpg         one still (fswebcam, exposure warm-up)
  GET  /clip.mp4?seconds=N   an N-second MP4 (default 10, max 120)
  GET  /api/info             node, device, camera name
  GET  /api/controls         current V4L2 control values
  POST /api/ptz              {pan_deg, tilt_deg, zoom}   (UVC ePTZ)
  POST /api/sensor           {brightness, contrast, saturation, gain, sharpness}

Image effects on snapshot/clip (query params, all optional):
  b   brightness  -1.0..1.0   (ffmpeg eq)
  c   contrast     0.0..2.0   (ffmpeg eq)
  g   gamma        0.1..4.0   (ffmpeg eq)
  sat saturation   0.0..3.0   (ffmpeg eq)
  il  input level low  0..0.9 (ffmpeg colorlevels)
  ih  input level high 0.1..1 (ffmpeg colorlevels)

Sensor-level brightness/contrast/etc. live under /api/sensor and change
what the camera itself produces; the query-param effects are applied to
the captured frame/clip and never touch the camera.

Registry for /grid: cameras.json next to this file or at
/etc/skysong/cameras.json - a list of {"name", "url"} entries.
"""
import argparse, json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DEVICE = os.environ.get("SKYSONG_DEVICE", "/dev/video0")
SNAP_RES = os.environ.get("SKYSONG_SNAP_RES", "3840x2160")
CLIP_RES = os.environ.get("SKYSONG_CLIP_RES", "1920x1080")
SNAP_SKIP = int(os.environ.get("SKYSONG_SNAP_SKIP", "90"))
HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()

PTZ = {"pan_absolute": (-36000, 36000), "tilt_absolute": (-36000, 36000),
       "zoom_absolute": (100, 500)}
SENSOR = ["brightness", "contrast", "saturation", "gain", "sharpness"]


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def camera_name():
    base = os.path.basename(DEVICE)
    try:
        with open(f"/sys/class/video4linux/{base}/name") as f:
            return f.read().strip()
    except OSError:
        return base


def get_ctrls(names):
    out = {}
    r = run(["v4l2-ctl", "-d", DEVICE, "--get-ctrl=" + ",".join(names)], 15)
    for line in r.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            try:
                out[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return out


def set_ctrls(pairs):
    args = []
    for k, v in pairs.items():
        args.append(f"--set-ctrl={k}={v}")
    r = run(["v4l2-ctl", "-d", DEVICE] + args, 15)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def fx_filter(q):
    """Build an ffmpeg -vf chain from effect query params."""
    parts = []
    eq = []
    if "b" in q: eq.append(f"brightness={float(q['b'][0]):.3f}")
    if "c" in q: eq.append(f"contrast={float(q['c'][0]):.3f}")
    if "g" in q: eq.append(f"gamma={float(q['g'][0]):.3f}")
    if "sat" in q: eq.append(f"saturation={float(q['sat'][0]):.3f}")
    if eq: parts.append("eq=" + ":".join(eq))
    if "il" in q or "ih" in q:
        il = float(q.get("il", [0])[0]); ih = float(q.get("ih", [1])[0])
        parts.append(f"colorlevels=rimin={il}:gimin={il}:bimin={il}"
                     f":rimax={ih}:gimax={ih}:bimax={ih}")
    return ",".join(parts) if parts else None


def capture_raw(path):
    r = run(["fswebcam", "-d", DEVICE, "-r", SNAP_RES, "--skip", str(SNAP_SKIP),
             "--jpeg", "92", "--no-banner", path], 120)
    return r.returncode == 0 and os.path.exists(path)


def apply_fx(src, dst, vf, is_video=False):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if vf: cmd += ["-vf", vf]
    if is_video:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", dst]
    else:
        cmd += ["-q:v", "3", dst]
    return run(cmd, 300).returncode == 0 and os.path.exists(dst)


def registry():
    for p in (os.path.join(HERE, "cameras.json"), "/etc/skysong/cameras.json"):
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return []


PAGE = """<!doctype html><meta charset=utf-8><title>{title}</title>
<style>body{{font-family:system-ui;background:#111;color:#eee;margin:2rem}}
img{{max-width:100%;background:#000}}input[type=range]{{width:220px}}
.row{{margin:.4rem 0}}button{{padding:.4rem 1rem}}</style>
<h1>{title}</h1>
<p>{node} &middot; {device} &middot; <a href="/grid" style="color:#8cf">grid of all cameras</a></p>
<img id=view src="/snapshot.jpg" alt="camera view">
<div class=row>
 <button onclick="snap()">Snapshot</button>
 <button onclick="clip()">Record 10s MP4</button>
 <span id=msg></span></div>
<h3>Pan / tilt / zoom (camera)</h3>
<div class=row>Pan <input type=range id=pan min=-36 max=36 step=1 value=0 oninput="ptz()"> <span id=panv>0&deg;</span></div>
<div class=row>Tilt <input type=range id=tilt min=-36 max=36 step=1 value=0 oninput="ptz()"> <span id=tiltv>0&deg;</span></div>
<div class=row>Zoom <input type=range id=zoom min=100 max=500 step=5 value=100 oninput="ptz()"> <span id=zoomv>1.0&times;</span></div>
<h3>Effects (applied to snapshot / clip)</h3>
<div class=row>Brightness <input type=range id=b min=-0.5 max=0.5 step=0.02 value=0></div>
<div class=row>Contrast <input type=range id=c min=0.4 max=1.8 step=0.02 value=1></div>
<div class=row>Gamma <input type=range id=g min=0.4 max=2.5 step=0.05 value=1></div>
<div class=row>Levels in <input type=range id=il min=0 max=0.6 step=0.01 value=0> &hellip; <input type=range id=ih min=0.4 max=1 step=0.01 value=1></div>
<script>
const $=id=>document.getElementById(id);
function fx(){{const p=new URLSearchParams();
 if($('b').value!=0)p.set('b',$('b').value); if($('c').value!=1)p.set('c',$('c').value);
 if($('g').value!=1)p.set('g',$('g').value);
 if($('il').value!=0)p.set('il',$('il').value); if($('ih').value!=1)p.set('ih',$('ih').value);
 return p.toString();}}
function snap(){{$('view').src='/snapshot.jpg?'+fx()+'&t='+Date.now();}}
function clip(){{$('msg').textContent='recording...';
 fetch('/clip.mp4?seconds=10&'+fx()).then(r=>r.blob()).then(b=>{{
  const a=document.createElement('a');a.href=URL.createObjectURL(b);
  a.download='clip.mp4';a.click();$('msg').textContent='clip downloaded';}});}}
let ptzTimer;
function ptz(){{$('panv').textContent=$('pan').value+'\u00B0';$('tiltv').textContent=$('tilt').value+'\u00B0';
 $('zoomv').textContent=($('zoom').value/100).toFixed(1)+'\u00D7';
 clearTimeout(ptzTimer);ptzTimer=setTimeout(()=>fetch('/api/ptz',{{method:'POST',
 headers:{{'Content-Type':'application/json'}},body:JSON.stringify(
 {{pan_deg:+$('pan').value,tilt_deg:+$('tilt').value,zoom:+$('zoom').value}})}}),150);}}
</script>"""

GRID = """<!doctype html><meta charset=utf-8><title>Skysong cameras</title>
<style>body{{font-family:system-ui;background:#111;color:#eee;margin:2rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:1rem}}
.tile img{{width:100%;background:#000}}.tile a{{color:#8cf}}</style>
<h1>Skysong &mdash; camera portals</h1>
<div class=grid>{tiles}</div>
<script>setInterval(()=>document.querySelectorAll('img').forEach(i=>{{
 const u=new URL(i.src);u.searchParams.set('t',Date.now());i.src=u;}}),15000);</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == "/":
                html = PAGE.format(title=camera_name(),
                                   node=os.uname().nodename, device=DEVICE)
                self.send_bytes(html.encode(), "text/html; charset=utf-8")
            elif u.path == "/grid":
                tiles = "".join(
                    f'<div class=tile><h3>{c["name"]}</h3>'
                    f'<a href="{c["url"]}/"><img src="{c["url"]}/snapshot.jpg"></a>'
                    f'<p><a href="{c["url"]}/">open portal</a></p></div>'
                    for c in registry())
                self.send_bytes(GRID.format(tiles=tiles or "<p>No cameras registered.</p>").encode(),
                                "text/html; charset=utf-8")
            elif u.path == "/api/info":
                self.send_json({"node": os.uname().nodename, "device": DEVICE,
                                "camera": camera_name()})
            elif u.path == "/api/controls":
                self.send_json(get_ctrls(list(PTZ) + SENSOR))
            elif u.path == "/snapshot.jpg":
                self.snapshot(q)
            elif u.path == "/clip.mp4":
                self.clip(q)
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:  # keep the agent up; report the failure
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if u.path == "/api/ptz":
            pairs = {}
            if "pan_deg" in body: pairs["pan_absolute"] = int(body["pan_deg"]) * 100
            if "tilt_deg" in body: pairs["tilt_absolute"] = int(body["tilt_deg"]) * 100
            if "zoom" in body: pairs["zoom_absolute"] = int(body["zoom"])
            for k, v in pairs.items():
                lo, hi = PTZ[k]
                pairs[k] = max(lo, min(hi, v))
            ok, msg = set_ctrls(pairs)
            self.send_json({"ok": ok, "set": pairs, "detail": msg}, 200 if ok else 500)
        elif u.path == "/api/sensor":
            pairs = {k: int(body[k]) for k in SENSOR if k in body}
            ok, msg = set_ctrls(pairs)
            self.send_json({"ok": ok, "set": pairs, "detail": msg}, 200 if ok else 500)
        else:
            self.send_json({"error": "not found"}, 404)

    def snapshot(self, q):
        with LOCK, tempfile.TemporaryDirectory() as td:
            raw, out = os.path.join(td, "raw.jpg"), os.path.join(td, "out.jpg")
            if not capture_raw(raw):
                return self.send_json({"error": "capture failed"}, 500)
            vf = fx_filter(q)
            if vf and not apply_fx(raw, out, vf):
                return self.send_json({"error": "effects failed"}, 500)
            with open(out if vf else raw, "rb") as f:
                self.send_bytes(f.read(), "image/jpeg")

    def clip(self, q):
        secs = max(1, min(120, int(q.get("seconds", [10])[0])))
        with LOCK, tempfile.TemporaryDirectory() as td:
            raw, out = os.path.join(td, "raw.mp4"), os.path.join(td, "out.mp4")
            r = run(["ffmpeg", "-y", "-loglevel", "error", "-f", "v4l2",
                     "-input_format", "mjpeg", "-video_size", CLIP_RES,
                     "-i", DEVICE, "-t", str(secs), "-c:v", "libx264",
                     "-preset", "veryfast", "-pix_fmt", "yuv420p", raw], 300)
            if r.returncode != 0 or not os.path.exists(raw):
                return self.send_json({"error": "record failed: " + r.stderr[-300:]}, 500)
            vf = fx_filter(q)
            src = raw
            if vf:
                if not apply_fx(raw, out, vf, is_video=True):
                    return self.send_json({"error": "effects failed"}, 500)
                src = out
            with open(src, "rb") as f:
                self.send_bytes(f.read(), "video/mp4")


def tailscale_ip():
    r = run(["tailscale", "ip", "-4"], 10)
    ip = r.stdout.strip().splitlines()
    return ip[0] if ip else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    args = ap.parse_args()
    hosts = ["127.0.0.1"]
    ts = tailscale_ip()
    if ts: hosts.append(ts)
    servers = []
    for h in hosts:
        srv = ThreadingHTTPServer((h, args.port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        print(f"skysong-agent on http://{h}:{args.port} device={DEVICE}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
