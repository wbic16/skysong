#!/usr/bin/env python3
"""skysong-agent v2 - one agent per camera host. Same interface on every camera.

Endpoints (default port 8642, bound to 127.0.0.1 + the node's tailnet IP):
  GET  /                     this camera's portal page
  GET  /grid                 grid portal of all registered cameras
  GET  /snapshot.jpg         the cached latest frame (instant); ?fresh=1
                             forces a live capture; effect params apply
                             to whichever frame is served
  GET  /reference.jpg        the most recent NOVEL frame - the latest
                             capture that differs from the baseline scene
  GET  /clip.mp4?seconds=N   an N-second MP4 (default 10, max 120)
  GET  /api/info             node, device, camera name
  GET  /api/status           cache + novelty state (ages, scores, counts)
  GET  /api/controls         current V4L2 control values
  POST /api/ptz              {pan_deg, tilt_deg, zoom}   (UVC ePTZ)
  POST /api/sensor           {brightness, contrast, saturation, gain, sharpness}

Screenshot cache + novelty (the service's memory):
  A background loop captures a frame every SKYSONG_CACHE_SECONDS (15).
  Each frame gets a tiny grayscale signature; a running baseline (an
  exponential average of non-novel signatures, persisted to disk) is
  the scene the camera usually sees. A frame whose mean absolute
  difference from baseline reaches SKYSONG_NOVELTY_THRESHOLD (10/255)
  is NOVEL: it becomes the reference image. Novelty is the priority -
  the reference image is always the most recent non-baseline frame,
  and /snapshot.jpg serves the cache so a recent image is instant.

Image effects on snapshot/clip (query params, all optional):
  b   brightness  -1.0..1.0   (ffmpeg eq)
  c   contrast     0.0..2.0   (ffmpeg eq)
  g   gamma        0.1..4.0   (ffmpeg eq)
  sat saturation   0.0..3.0   (ffmpeg eq)
  il  input level low  0..0.9 (ffmpeg colorlevels)
  ih  input level high 0.1..1 (ffmpeg colorlevels)

Sensor-level brightness/contrast/etc. live under /api/sensor and change
what the camera itself produces; the query-param effects are applied to
the served frame and never touch the camera.

Registry for /grid: cameras.json next to this file or at
/etc/skysong/cameras.json - a list of {"name", "url"} entries.
"""
import argparse, json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DEVICE = os.environ.get("SKYSONG_DEVICE", "/dev/video0")
SNAP_RES = os.environ.get("SKYSONG_SNAP_RES", "3840x2160")
CLIP_RES = os.environ.get("SKYSONG_CLIP_RES", "1920x1080")
SNAP_SKIP = int(os.environ.get("SKYSONG_SNAP_SKIP", "90"))
CACHE_RES = os.environ.get("SKYSONG_CACHE_RES", "1280x720")
CACHE_SKIP = int(os.environ.get("SKYSONG_CACHE_SKIP", "30"))
CACHE_SECONDS = float(os.environ.get("SKYSONG_CACHE_SECONDS", "15"))
NOVELTY_THRESHOLD = float(os.environ.get("SKYSONG_NOVELTY_THRESHOLD", "10"))
SIG_W, SIG_H = 32, 18
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
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


def capture_raw(path, res=SNAP_RES, skip=SNAP_SKIP):
    r = run(["fswebcam", "-d", DEVICE, "-r", res, "--skip", str(skip),
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


def signature(path):
    """Tiny grayscale signature of a frame: SIG_W*SIG_H bytes, or None."""
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path,
                        "-vf", f"scale={SIG_W}:{SIG_H},format=gray",
                        "-f", "rawvideo", "-"],
                       capture_output=True, timeout=60)
    if r.returncode == 0 and len(r.stdout) == SIG_W * SIG_H:
        return r.stdout
    return None


class FrameCache:
    """Latest frame + baseline + the most recent novel (reference) frame."""

    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.latest = os.path.join(CACHE_DIR, "latest.jpg")
        self.reference = os.path.join(CACHE_DIR, "reference.jpg")
        self.baseline_file = os.path.join(CACHE_DIR, "baseline.raw")
        self.lock = threading.Lock()
        self.baseline = None          # list of floats, len SIG_W*SIG_H
        self.latest_ts = 0.0
        self.reference_ts = 0.0
        self.last_score = None
        self.captures = 0
        self.novel_count = 0
        self.events = []              # recent novel events {ts, score}
        if os.path.exists(self.baseline_file):
            raw = open(self.baseline_file, "rb").read()
            if len(raw) == SIG_W * SIG_H:
                self.baseline = [float(b) for b in raw]
        if os.path.exists(self.reference):
            self.reference_ts = os.path.getmtime(self.reference)
        if os.path.exists(self.latest):
            self.latest_ts = os.path.getmtime(self.latest)

    def consider(self, frame_path, sig):
        """Score a new frame against baseline; promote it if novel."""
        now = time.time()
        with self.lock:
            self.captures += 1
            self.latest_ts = now
            if sig is None:
                return
            if self.baseline is None:
                self.baseline = [float(b) for b in sig]
                self._save_baseline()
                return
            score = sum(abs(a - b) for a, b in zip(sig, self.baseline)) / len(sig)
            self.last_score = round(score, 2)
            if score >= NOVELTY_THRESHOLD:
                # Novel: this frame is the new reference. The baseline
                # does NOT chase it - novelty stays the priority.
                with open(frame_path, "rb") as f:
                    data = f.read()
                tmp = self.reference + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, self.reference)
                self.reference_ts = now
                self.novel_count += 1
                self.events.append({"ts": now, "score": round(score, 2)})
                self.events = self.events[-20:]
            else:
                # Ordinary frame: let the baseline drift slowly toward
                # the settled scene so slow changes (dusk, seasons)
                # become the new normal instead of permanent novelty.
                a = 0.05
                self.baseline = [b + a * (s - b) for b, s in zip(self.baseline, sig)]
                self._save_baseline()

    def _save_baseline(self):
        raw = bytes(max(0, min(255, round(v))) for v in self.baseline)
        tmp = self.baseline_file + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, self.baseline_file)

    def status(self):
        now = time.time()
        with self.lock:
            return {
                "captures": self.captures,
                "novel_count": self.novel_count,
                "last_score": self.last_score,
                "threshold": NOVELTY_THRESHOLD,
                "baseline_ready": self.baseline is not None,
                "latest_ts": self.latest_ts or None,
                "latest_age_s": round(now - self.latest_ts, 1) if self.latest_ts else None,
                "reference_ts": self.reference_ts or None,
                "reference_age_s": round(now - self.reference_ts, 1) if self.reference_ts else None,
                "recent_novel_events": self.events[-5:],
            }


CACHE = FrameCache()


def cache_loop():
    """Capture forever: latest frame into the cache, novelty scored."""
    while True:
        try:
            with LOCK, tempfile.TemporaryDirectory() as td:
                frame = os.path.join(td, "frame.jpg")
                if capture_raw(frame, res=CACHE_RES, skip=CACHE_SKIP):
                    tmp = CACHE.latest + ".tmp"
                    with open(frame, "rb") as f:
                        data = f.read()
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, CACHE.latest)
                    CACHE.consider(CACHE.latest, signature(CACHE.latest))
        except Exception as e:
            print(f"cache loop: {e}", file=sys.stderr, flush=True)
        time.sleep(CACHE_SECONDS)


def registry():
    for p in (os.path.join(HERE, "cameras.json"), "/etc/skysong/cameras.json"):
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return []


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0b0e13;color:#e8ecf1;
 margin:0;padding:1.2rem 1.4rem 2.5rem;max-width:1180px;margin-inline:auto}
a{color:#7cc4ff}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6rem 1rem;margin-bottom:1rem}
h1{font-size:1.35rem;margin:0}
.sub{color:#9aa7b5;font-size:.9rem}
.pill{font-size:.75rem;font-weight:600;padding:.18rem .6rem;border-radius:999px;
 background:#17351f;color:#7de08d;border:1px solid #2c5c37}
.pill.stale{background:#3a2b12;color:#f2c14e;border-color:#6b5518}
.pill.novel{background:#3d1530;color:#ff9ad5;border-color:#7a2c5e}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.card{background:#12161d;border:1px solid #232b36;border-radius:12px;padding:.9rem}
.card h2{font-size:.95rem;margin:.1rem 0 .5rem;color:#c8d3df}
.card img{width:100%;border-radius:8px;background:#000;display:block}
.cap{color:#9aa7b5;font-size:.82rem;margin-top:.45rem}
.cap b{color:#e8ecf1}
button{background:#1d6fd1;color:#fff;border:0;border-radius:8px;padding:.5rem 1rem;
 font-size:.9rem;cursor:pointer}
button:hover{background:#2a80e4}
button.ghost{background:#1a222d;color:#c8d3df;border:1px solid #2b3644}
.row{display:flex;align-items:center;gap:.7rem;margin:.45rem 0;flex-wrap:wrap}
.row label{width:110px;color:#9aa7b5;font-size:.85rem}
input[type=range]{flex:1;min-width:140px}
.val{width:52px;text-align:right;font-variant-numeric:tabular-nums;font-size:.85rem}
.controls{margin-top:1rem}
h3{font-size:.9rem;color:#c8d3df;margin:1rem 0 .3rem}
#msg{color:#9aa7b5;font-size:.85rem}
.presetbar{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
button.preset{background:#1a222d;color:#c8d3df;border:1px solid #2b3644}
button.preset:hover{background:#232f3d}
button.preset.active{background:#7a2c5e;color:#ffd7ef;border-color:#a8458a}
.tabs{display:flex;gap:.5rem;margin:1rem 0 .6rem}
button.tab{background:transparent;color:#9aa7b5;border:1px solid #2b3644}
button.tab.active{background:#1d6fd1;color:#fff;border-color:#1d6fd1}
"""

PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title} - camera</title><style>{css}</style>
<header>
 <h1>{title}</h1><span class=sub>{node} &middot; {device}</span>
 <span class=pill id=livepill>connecting</span>
 <span class=pill novel id=novelpill style="display:none"></span>
 <span class=sub style="margin-left:auto"><a href="/grid">grid of all cameras</a></span>
</header>
<div class=grid2>
 <div class=card>
  <h2>Most recent change (reference image)</h2>
  <img id=ref src="/reference.jpg" alt="most recent novel frame">
  <div class=cap id=refcap>waiting for the first novel frame&hellip;</div>
 </div>
 <div class=card>
  <h2>Latest frame (cached, refreshes automatically)</h2>
  <img id=view src="/snapshot.jpg" alt="latest cached frame">
  <div class=cap id=viewcap>&hellip;</div>
 </div>
</div>
<div class="card" style="margin-top:1rem">
 <div class=presetbar>
  <b style="color:#c8d3df">Presets</b>
  <button class=preset data-preset=night onclick="applyPreset('night')">Night</button>
  <button class=preset data-preset=day onclick="applyPreset('day')">Day</button>
  <button class=preset data-preset=dawn onclick="applyPreset('dawn')">Dawn</button>
  <button class=preset data-preset=dusk onclick="applyPreset('dusk')">Dusk</button>
  <button class=preset data-preset=llama onclick="applyPreset('llama')">&#129433; Llama Search</button>
  <span style="flex:1"></span>
  <button onclick="fresh()">Fresh snapshot</button>
  <button class=ghost onclick="clip()">Record 10s MP4</button>
  <span id=msg></span>
 </div>
</div>
<div class=tabs>
 <button class="tab active" id=tabbtn-view onclick="showTab('view')">View</button>
 <button class=tab id=tabbtn-adv onclick="showTab('adv')">Advanced</button>
</div>
<div id=tab-view></div>
<div id=tab-adv style="display:none">
<div class="card controls">
 <h3>Pan / tilt / zoom (camera)</h3>
 <div class=row><label>Pan</label><input type=range id=pan min=-36 max=36 step=1 value=0 oninput="ptz()"><span class=val id=panv>0&deg;</span></div>
 <div class=row><label>Tilt</label><input type=range id=tilt min=-36 max=36 step=1 value=0 oninput="ptz()"><span class=val id=tiltv>0&deg;</span></div>
 <div class=row><label>Zoom</label><input type=range id=zoom min=100 max=500 step=5 value=100 oninput="ptz()"><span class=val id=zoomv>1.0&times;</span></div>
 <h3>Effects (applied to the served frame, never to the camera)</h3>
 <div class=row><label>Brightness</label><input type=range id=b min=-0.5 max=0.5 step=0.02 value=0 oninput="manualFx()"></div>
 <div class=row><label>Contrast</label><input type=range id=c min=0.4 max=1.8 step=0.02 value=1 oninput="manualFx()"></div>
 <div class=row><label>Gamma</label><input type=range id=g min=0.4 max=2.5 step=0.05 value=1 oninput="manualFx()"></div>
 <div class=row><label>Saturation</label><input type=range id=sat min=0 max=3 step=0.05 value=1 oninput="manualFx()"></div>
 <div class=row><label>Levels in</label><input type=range id=il min=0 max=0.6 step=0.01 value=0 oninput="manualFx()"> <input type=range id=ih min=0.4 max=1 step=0.01 value=1 oninput="manualFx()"></div>
 <h3>Sensor (changes what the camera itself produces)</h3>
 <div class=row><label>Brightness</label><input type=range id=sb min=0 max=255 step=1 value=128></div>
 <div class=row><label>Contrast</label><input type=range id=sc min=0 max=255 step=1 value=128></div>
 <div class=row><label>Saturation</label><input type=range id=ssat min=0 max=255 step=1 value=128></div>
 <div class=row><label>Gain</label><input type=range id=sgain min=0 max=255 step=1 value=128></div>
 <div class=row><label>Sharpness</label><input type=range id=ssharp min=0 max=255 step=1 value=128></div>
 <div class=row><button class=ghost onclick="applySensor()">Apply sensor</button>
 <button class=ghost onclick="resetSensor()">Sensor defaults</button><span id=smsg></span></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmtT=ts=>ts?new Date(ts*1000).toLocaleTimeString():'-';
const fmtAge=s=>s==null?'-':(s<90?Math.round(s)+'s ago':Math.round(s/60)+'m ago');
function fx(){{const p=new URLSearchParams();
 if($('b').value!=0)p.set('b',$('b').value); if($('c').value!=1)p.set('c',$('c').value);
 if($('g').value!=1)p.set('g',$('g').value); if($('sat').value!=1)p.set('sat',$('sat').value);
 if($('il').value!=0)p.set('il',$('il').value); if($('ih').value!=1)p.set('ih',$('ih').value);
 return p.toString();}}
function snap(){{const f=fx();
 $('view').src='/snapshot.jpg?'+f+'&t='+Date.now();
 $('ref').src='/reference.jpg?'+f+'&t='+Date.now();}}
function manualFx(){{document.querySelectorAll('button.preset').forEach(x=>x.classList.remove('active'));snap();}}
const PRESETS={{
 night:{{b:0.25,c:1.10,g:2.20,sat:1.00,il:0.00,ih:0.75}},
 day:{{b:0.00,c:1.05,g:1.00,sat:1.00,il:0.00,ih:1.00}},
 dawn:{{b:0.10,c:1.15,g:1.50,sat:1.25,il:0.00,ih:0.90}},
 dusk:{{b:0.15,c:1.10,g:1.70,sat:1.15,il:0.00,ih:0.85}},
 llama:{{b:0.30,c:1.35,g:2.40,sat:1.50,il:0.02,ih:0.80}}}};
function applyPreset(name){{const p=PRESETS[name];if(!p)return;
 $('b').value=p.b;$('c').value=p.c;$('g').value=p.g;$('sat').value=p.sat;
 $('il').value=p.il;$('ih').value=p.ih;
 document.querySelectorAll('button.preset').forEach(x=>x.classList.toggle('active',x.dataset.preset===name));
 snap();}}
function showTab(which){{const adv=which==='adv';
 $('tab-adv').style.display=adv?'':'none';
 $('tabbtn-view').classList.toggle('active',!adv);
 $('tabbtn-adv').classList.toggle('active',adv);
 if(adv)loadSensor();}}
let sensorLoaded=false;
async function loadSensor(){{if(sensorLoaded)return;sensorLoaded=true;
 try{{const v=await (await fetch('/api/controls')).json();
  if(v.brightness!=null)$('sb').value=v.brightness; if(v.contrast!=null)$('sc').value=v.contrast;
  if(v.saturation!=null)$('ssat').value=v.saturation; if(v.gain!=null)$('sgain').value=v.gain;
  if(v.sharpness!=null)$('ssharp').value=v.sharpness;}}catch(e){{}}}}
function postSensor(vals){{return fetch('/api/sensor',{{method:'POST',
 headers:{{'Content-Type':'application/json'}},body:JSON.stringify(vals)}});}}
function applySensor(){{postSensor({{brightness:+$('sb').value,contrast:+$('sc').value,
 saturation:+$('ssat').value,gain:+$('sgain').value,sharpness:+$('ssharp').value}})
 .then(r=>r.json()).then(j=>{{$('smsg').textContent=j.ok?'sensor applied':'sensor failed';}});}}
function resetSensor(){{$('sb').value=128;$('sc').value=128;$('ssat').value=128;
 $('sgain').value=128;$('ssharp').value=128;applySensor();}}
function fresh(){{$('msg').textContent='capturing...';
 const img=new Image();
 img.onload=()=>{{$('view').src=img.src;$('msg').textContent='fresh frame loaded';}};
 img.src='/snapshot.jpg?fresh=1&'+fx()+'&t='+Date.now();}}
function clip(){{$('msg').textContent='recording...';
 fetch('/clip.mp4?seconds=10&'+fx()).then(r=>r.blob()).then(b=>{{
  const a=document.createElement('a');a.href=URL.createObjectURL(b);
  a.download='clip.mp4';a.click();$('msg').textContent='clip downloaded';}});}}
let ptzTimer;
function ptz(){{$('panv').textContent=$('pan').value+'\\u00B0';$('tiltv').textContent=$('tilt').value+'\\u00B0';
 $('zoomv').textContent=($('zoom').value/100).toFixed(1)+'\\u00D7';
 clearTimeout(ptzTimer);ptzTimer=setTimeout(()=>fetch('/api/ptz',{{method:'POST',
 headers:{{'Content-Type':'application/json'}},body:JSON.stringify(
 {{pan_deg:+$('pan').value,tilt_deg:+$('tilt').value,zoom:+$('zoom').value}})}}),150);}}
let lastRefTs=0;
async function tick(){{
 try{{
  const s=await (await fetch('/api/status')).json();
  const live=s.latest_age_s!=null&&s.latest_age_s<45;
  $('livepill').textContent=live?('live - frame '+fmtAge(s.latest_age_s)):('stale - frame '+fmtAge(s.latest_age_s));
  $('livepill').className='pill'+(live?'':' stale');
  $('viewcap').innerHTML='captured <b>'+fmtT(s.latest_ts)+'</b> &middot; change score <b>'+
    (s.last_score==null?'-':s.last_score)+'</b> / '+s.threshold+' &middot; '+s.captures+' frames cached';
  if(s.reference_ts){{
   $('novelpill').style.display='';
   $('novelpill').textContent='last change '+fmtAge(s.reference_age_s);
   $('refcap').innerHTML='captured <b>'+fmtT(s.reference_ts)+'</b> &middot; '+
     fmtAge(s.reference_age_s)+' &middot; '+s.novel_count+' novel frames so far';
   if(s.reference_ts!==lastRefTs){{lastRefTs=s.reference_ts;
     $('ref').src='/reference.jpg?'+fx()+'&t='+Date.now();}}
  }}
 }}catch(e){{ $('livepill').textContent='agent unreachable'; $('livepill').className='pill stale'; }}
 snap();
}}
setInterval(tick,5000); tick();
</script>"""

GRID = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Camera grid</title><style>{css}
.tiles{{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:1rem}}
.tile img{{width:100%;border-radius:8px;background:#000;display:block}}
.tile h2 a{{color:#e8ecf1;text-decoration:none}}
</style>
<header><h1>Camera grid</h1><span class=sub>every registered camera, one portal each</span></header>
<div class=tiles>{tiles}</div>
<script>
async function tileStatus(el,base){{
 try{{const s=await (await fetch(base+'/api/status')).json();
  el.textContent='frame '+(s.latest_age_s==null?'-':Math.round(s.latest_age_s)+'s old')+
   ' - last change '+(s.reference_ts?new Date(s.reference_ts*1000).toLocaleTimeString():'none yet');
 }}catch(e){{el.textContent='status unreachable';}}}}
document.querySelectorAll('.tile').forEach(t=>tileStatus(t.querySelector('.st'),t.dataset.base));
setInterval(()=>document.querySelectorAll('.tile img').forEach(i=>{{
 const u=new URL(i.src);u.searchParams.set('t',Date.now());i.src=u;}}),15000);
setInterval(()=>document.querySelectorAll('.tile').forEach(t=>tileStatus(t.querySelector('.st'),t.dataset.base)),30000);
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", code)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            self.send_bytes(f.read(), ctype)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == "/":
                html = PAGE.format(css=CSS, title=camera_name(),
                                   node=os.uname().nodename, device=DEVICE)
                self.send_bytes(html.encode(), "text/html; charset=utf-8")
            elif u.path == "/grid":
                tiles = "".join(
                    f'<div class="card tile" data-base="{c["url"]}">'
                    f'<h2><a href="{c["url"]}/">{c["name"]}</a></h2>'
                    f'<a href="{c["url"]}/"><img src="{c["url"]}/snapshot.jpg"></a>'
                    f'<div class="cap st">checking&hellip;</div></div>'
                    for c in registry())
                self.send_bytes(GRID.format(css=CSS, tiles=tiles or "<p>No cameras registered.</p>").encode(),
                                "text/html; charset=utf-8")
            elif u.path == "/api/info":
                self.send_json({"node": os.uname().nodename, "device": DEVICE,
                                "camera": camera_name()})
            elif u.path == "/api/status":
                self.send_json(CACHE.status())
            elif u.path == "/api/controls":
                self.send_json(get_ctrls(list(PTZ) + SENSOR))
            elif u.path == "/snapshot.jpg":
                self.snapshot(q)
            elif u.path == "/reference.jpg":
                self.reference(q)
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

    def _serve_frame(self, src_path, q):
        vf = fx_filter(q)
        if not vf:
            return self.send_file(src_path, "image/jpeg")
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.jpg")
            if not apply_fx(src_path, out, vf):
                return self.send_json({"error": "effects failed"}, 500)
            self.send_file(out, "image/jpeg")

    def snapshot(self, q):
        if q.get("fresh", ["0"])[0] == "1":
            with LOCK, tempfile.TemporaryDirectory() as td:
                raw = os.path.join(td, "raw.jpg")
                if not capture_raw(raw):
                    return self.send_json({"error": "capture failed"}, 500)
                return self._serve_frame(raw, q)
        if not os.path.exists(CACHE.latest):
            return self.send_json({"error": "cache not warm yet"}, 503)
        self._serve_frame(CACHE.latest, q)

    def reference(self, q):
        path = CACHE.reference if os.path.exists(CACHE.reference) else CACHE.latest
        if not os.path.exists(path):
            return self.send_json({"error": "no frames yet"}, 503)
        self._serve_frame(path, q)

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
    threading.Thread(target=cache_loop, daemon=True).start()
    hosts = ["127.0.0.1"]
    ts = tailscale_ip()
    if ts: hosts.append(ts)
    servers = []
    for h in hosts:
        srv = ThreadingHTTPServer((h, args.port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        print(f"skysong-agent v2 on http://{h}:{args.port} device={DEVICE} "
              f"cache={CACHE_SECONDS}s threshold={NOVELTY_THRESHOLD}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
