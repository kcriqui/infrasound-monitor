#!/usr/bin/env python3
"""Lightweight live-status web page for the INFRA20 monitor.

Serves a small auto-refreshing status page (and a JSON endpoint) so you can check the
monitor from a browser instead of SSHing in: last-sample age, live level, dominant tone,
uptime, today's data, disk, CPU temp, last publish, and a live waveform. It reuses the
exact cheap collectors the PiTFT display uses (live.npz + /proc + file mtimes) -- no
obspy, no new dependencies, just the stdlib http.server.

Endpoints:
    /              auto-refreshing HTML status page
    /status.json   the same data as JSON (for scripts / other dashboards)
    /healthz       "ok" (200) for uptime checks

Runs as the infra-status systemd service. It binds 0.0.0.0 by default and has NO
authentication -- it's meant for a PRIVATE network (home LAN / a VPN like Tailscale).
Do NOT port-forward it to the public internet.

    python tools/statusd.py --port 8080
"""
from __future__ import annotations
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

# Reuse the display's collectors (sibling module in tools/). Importing it does NOT pull in
# board/adafruit -- those load lazily only when a real panel is initialised.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tft_status as T  # noqa: E402

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INFRA20 status</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0c0e12;color:#e6e9ee;font:15px/1.4 system-ui,sans-serif}
 .wrap{max-width:640px;margin:0 auto;padding:20px}
 header{display:flex;align-items:center;gap:12px;margin-bottom:4px}
 h1{font-size:18px;margin:0;color:#5aaaf5;font-weight:600}
 .site{color:#8890a0;font-size:13px;margin:0 0 16px}
 .pill{padding:3px 12px;border-radius:999px;font-weight:700;font-size:13px}
 .ok{background:#123d24;color:#3cc86e}.bad{background:#3d1616;color:#ec5555}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
 .tile{background:#161a21;border:1px solid #232833;border-radius:10px;padding:10px 12px}
 .k{color:#8890a0;font-size:12px}.v{font-size:20px;font-weight:600;margin-top:2px}
 canvas{width:100%;height:120px;background:#161a21;border:1px solid #232833;border-radius:10px;display:block}
 .foot{color:#697084;font-size:12px;margin-top:14px}
 .stale{opacity:.5}
</style></head><body><div class="wrap">
 <header><h1 id="station">INFRA20</h1><span id="pill" class="pill">…</span></header>
 <p class="site" id="site"></p>
 <canvas id="wave" width="600" height="120"></canvas>
 <div class="grid" id="grid"></div>
 <p class="foot" id="foot">connecting…</p>
</div>
<script>
const TILES=[["age","last sample"],["level","level"],["tone","tone"],["up","uptime"],
 ["today","today"],["archive","archive"],["disk","disk free"],["cpu","cpu"],["publish","last publish"]];
function fmt(d){return{
 age:d.age?d.age+" ago":"--",
 level:d.level_pa!=null?d.level_pa.toFixed(3)+" Pa":"--",
 tone:d.tone_hz!=null?d.tone_hz.toFixed(1)+" Hz":"--",
 up:d.uptime||"--",
 today:d.today_mb!=null?d.today_mb.toFixed(1)+" MB":"--",
 archive:d.archive_gb!=null?d.archive_gb.toFixed(2)+" GB":"--",
 disk:d.disk_free_pct!=null?d.disk_free_pct.toFixed(0)+"%":"--",
 cpu:d.cpu_c!=null?d.cpu_c.toFixed(0)+" °C":"--",
 publish:d.publish_age?d.publish_age+" ago":"--"};}
function drawWave(w){const c=document.getElementById("wave"),x=c.getContext("2d");
 const W=c.width,H=c.height;x.clearRect(0,0,W,H);
 x.strokeStyle="#1e242e";x.beginPath();x.moveTo(0,H/2);x.lineTo(W,H/2);x.stroke();
 if(!w||w.length<2)return;let a=0;for(const v of w)a=Math.max(a,Math.abs(v));a=a||1e-6;
 x.strokeStyle="#5aaaf5";x.lineWidth=1.5;x.beginPath();
 for(let i=0;i<w.length;i++){const px=i*W/(w.length-1),py=H/2-(w[i]/a)*(H/2-4);
  i?x.lineTo(px,py):x.moveTo(px,py);}x.stroke();}
async function tick(){try{
 const d=await(await fetch("status.json",{cache:"no-store"})).json();
 document.getElementById("station").textContent=d.station;
 document.getElementById("site").textContent=d.site||"";
 const p=document.getElementById("pill");p.textContent=d.state;
 p.className="pill "+(d.ok?"ok":"bad");
 const f=fmt(d),g=document.getElementById("grid");g.innerHTML="";
 for(const[k,label]of TILES){const t=document.createElement("div");t.className="tile";
  t.innerHTML='<div class="k">'+label+'</div><div class="v">'+f[k]+'</div>';g.appendChild(t);}
 drawWave(d.wave);
 document.body.classList.toggle("stale",!d.ok);
 document.getElementById("foot").textContent="updated "+new Date().toLocaleTimeString();
}catch(e){document.getElementById("foot").textContent="disconnected — retrying…";}}
tick();setInterval(tick,3000);
</script></body></html>"""


def collect(site_index):
    live, sysm = T.read_live(), T.read_system()
    arch, pub = T.read_archive(), T.read_publish(site_index)
    ok = bool(live["present"] and live["age_s"] is not None and live["age_s"] < T.STALE_S)
    wave = live.get("wave")
    wave_list = None
    if wave is not None and len(wave) > 1:
        w = np.asarray(wave, dtype=float)
        if w.size > 240:                          # downsample for the browser
            w = w[np.linspace(0, w.size - 1, 240).astype(int)]
        wave_list = [round(float(v), 4) for v in w]
    return {
        "station": f"{T.DEFAULT_STATION.network}.{T.DEFAULT_STATION.station}",
        "site": T.DEFAULT_STATION.site_name,
        "ok": ok,
        "state": "OK" if ok else ("STALE" if live["present"] else "NO DATA"),
        "age_s": live["age_s"], "age": T.fmt_age(live["age_s"]) if live["present"] else None,
        "level_pa": live.get("rms_pa"),
        "tone_hz": live.get("dom_hz"),
        "uptime": T.fmt_dur(sysm.get("uptime_s")),
        "today_mb": arch.get("today_mb"),
        "archive_gb": arch.get("total_gb"),
        "disk_free_pct": sysm.get("disk_free_pct"),
        "cpu_c": sysm.get("cpu_c"),
        "publish_age": T.fmt_age(pub) if pub is not None else None,
        "wave": wave_list,
    }


def make_handler(site_index):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(PAGE, "text/html; charset=utf-8")
            elif path == "/status.json":
                self._send(json.dumps(collect(site_index)), "application/json")
            elif path == "/healthz":
                self._send("ok", "text/plain")
            else:
                self.send_error(404)

        def log_message(self, *args):             # keep the journal quiet
            pass

    return Handler


def main():
    p = argparse.ArgumentParser(description="INFRA20 live-status web page.")
    p.add_argument("--host", default="0.0.0.0", help="bind address (default all interfaces)")
    p.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    p.add_argument("--site-index", default=str(Path(T.PROJECT_ROOT) / "site" / "index.html"),
                   help="published index.html whose mtime is the 'last publish' time")
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(a.site_index))
    print(f"INFRA20 status server on http://{a.host}:{a.port}  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
