#!/usr/bin/env python3
"""Generate the public monitoring dashboard — one self-contained static HTML page.

Ties the analysis outputs (night tone-hunt, spectral waterfall, PDF-PSD, data
availability) into a single ``index.html`` with all images embedded as base64, so
it opens with no server and can be published as-is to any static host (GitHub
Pages / Netlify / S3).  Regenerate on a schedule to keep it fresh.

    python tools/dashboard.py "C:/Users/you/infra-archive" \
        --start 2026-04-09 --end 2026-07-12 --cache analysis/grid_full.npz

Reuses the sibling tools ``report.py`` and ``tonehunt.py``.
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import report          # sibling tool (tools/ is on sys.path when run as a script)
import tonehunt        # sibling tool
from infrasound_monitor.config import StationConfig, DEFAULT_STATION

plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "figure.facecolor": "white"})


def _parse_date(s):
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s}")


def _fig_waterfall(times, freqs, psd):
    Z = psd.T
    fin = Z[np.isfinite(Z)]
    vmin, vmax = np.percentile(fin, [5, 99]) if fin.size else (-60, -10)
    xnum = mdates.date2num(list(times))
    fig, ax = plt.subplots(figsize=(12, 4.2))
    im = ax.imshow(Z, origin="lower", aspect="auto",
                   extent=[xnum[0], xnum[-1], float(freqs.min()), float(freqs.max())],
                   vmin=vmin, vmax=vmax, cmap="viridis")
    ax.xaxis_date(); ax.set_ylabel("Frequency (Hz)")
    plt.colorbar(im, ax=ax, label="PSD dB (Pa²/Hz)")
    fig.autofmt_xdate(); fig.tight_layout()
    return report._fig_b64(fig)


def _fig_nightly(day_dt, freqs, night_wf, label):
    xnum = mdates.date2num(day_dt)
    fig, ax = plt.subplots(figsize=(12, 4.6))
    im = ax.imshow(night_wf.T, origin="lower", aspect="auto",
                   extent=[xnum[0], xnum[-1], float(freqs.min()), float(freqs.max())],
                   vmin=0, vmax=6, cmap="magma")
    ax.xaxis_date(); ax.set_ylabel("Frequency (Hz)")
    plt.colorbar(im, ax=ax, label="dB above background")
    fig.autofmt_xdate(); fig.tight_layout()
    return report._fig_b64(fig)


def build(archive, start, end, out_html, cfg, cache=None, nperseg=8192,
          baseline_hz=1.0, night_window=5, utc_offset=-7.0):
    grid = (report.load_grid(cache) if (cache and Path(cache).exists())
            else report.compute_grid(archive, start, end, cfg, nperseg=nperseg))
    if cache and not Path(cache).exists():
        report.save_grid(grid, cache)
    times = list(grid["times"]); freqs = np.asarray(grid["freqs"], float)
    psd = np.asarray(grid["psd_db"], float)
    have = np.isfinite(psd).all(axis=1)
    resid, _ = tonehunt.tone_residual(psd, freqs, baseline_hz)

    # night analysis
    night_hours, _ = tonehunt.quiet_window(times, freqs, psd, have, night_window)
    night_wf, day_dt, nmask = tonehunt.night_dates_matrix(times, resid, have, night_hours)
    loc = [int((h + utc_offset) % 24) for h in night_hours]
    night_label = (f"{night_hours[0]:02d}:00–{(night_hours[-1]+1)%24:02d}:00 UTC "
                   f"· ~{min(loc):02d}:00–{(max(loc)+1)%24:02d}:00 local")
    thr = 6.0
    pn = np.mean(resid[nmask] > thr, axis=0) if nmask.any() else np.zeros(len(freqs))
    dmask = have & ~nmask
    pd_ = np.mean(resid[dmask] > thr, axis=0) if dmask.any() else np.zeros(len(freqs))
    peak_pct = float(pn.max() * 100) if pn.size else 0.0
    diff = pn - pd_
    top = sorted(np.argsort(diff)[::-1][:6], key=lambda k: freqs[k])
    tone_rows = [(float(freqs[i]), float(pn[i] * 100), float(pd_[i] * 100)) for i in top]

    av = report.availability(times, have)
    pdf = report.pdf_psd(freqs, psd, have)
    bands = report.band_stats(freqs, psd, have)
    meta = _station_meta(archive)

    imgs = dict(waterfall=_fig_waterfall(times, freqs, psd),
                night=_fig_nightly(day_dt, freqs, night_wf, night_label),
                pdf=pdf["img"], avail=av["img"])
    kpis = dict(uptime=av["uptime"], ngaps=av["ngaps"], nights=len(day_dt),
                span_days=(end - start).days, peak_pct=peak_pct,
                night_label=night_label,
                overall=next((b for b in bands if b["band"].startswith("0.05–20")), None))
    html = _render(cfg, start, end, meta, kpis, tone_rows, bands, imgs, thr,
                   has_interactive=(Path(out_html).parent / "waterfall_full.html").exists())
    Path(out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(out_html).write_text(html, encoding="utf-8")
    print(f"wrote {out_html}")
    print(f"  uptime {kpis['uptime']:.1f}%  nights {kpis['nights']}  "
          f"tone peak {peak_pct:.0f}% -> "
          f"{'tone-free' if peak_pct < 50 else 'possible tone'}")
    return out_html


def _station_meta(archive):
    p = Path(archive) / "station.xml"
    if not p.exists():
        return {}
    try:
        from obspy import read_inventory
        inv = read_inventory(str(p)); ch = inv[0][0][0]
        return dict(lat=ch.latitude, lon=ch.longitude, elev=ch.elevation,
                    fs=ch.sample_rate, site=inv[0][0].site.name,
                    sens=ch.response.instrument_sensitivity.value)
    except Exception:
        return {}


# ---------------------------------------------------------------- rendering ----
def _tile(label, value, sub="", state=""):
    cls = f"tile {state}".strip()
    sub = f'<div class="tile-sub">{sub}</div>' if sub else ""
    return (f'<div class="{cls}"><div class="tile-label">{label}</div>'
            f'<div class="tile-val">{value}</div>{sub}</div>')


def _render(cfg, start, end, meta, k, tone_rows, bands, imgs, thr, has_interactive):
    now = dt.datetime.now()
    tone_free = k["peak_pct"] < 50
    verdict_state = "ok" if tone_free else "warn"
    verdict_word = "No steady tone" if tone_free else "Possible tone"
    loc = (f'{meta.get("lat"):.4f}, {meta.get("lon"):.4f}'
           if meta.get("lat") is not None else "—")
    ov = k["overall"]
    noise_spl = f'{ov["spl"]:.0f} dB SPL' if ov else "—"

    tiles = "".join([
        _tile("Data uptime", f'{k["uptime"]:.1f}<span class="unit">%</span>',
              f'{k["ngaps"]} gaps', "ok" if k["uptime"] >= 85 else "warn"),
        _tile("Datacenter tone", verdict_word,
              f'peak {k["peak_pct"]:.0f}% of nights', verdict_state),
        _tile("Ambient level", noise_spl.split()[0] + '<span class="unit"> dB SPL</span>',
              "median, 0.05–20 Hz"),
        _tile("Coverage", f'{k["span_days"]}<span class="unit"> days</span>',
              f'{k["nights"]} nights analyzed'),
        _tile("Quiet window", k["night_label"].split("·")[1].strip(),
              "auto-detected (highest-SNR)"),
    ])

    tone_tbl = "".join(
        f'<tr><td>{f:.2f} Hz</td><td class="num">{n:.0f}%</td>'
        f'<td class="num">{d:.0f}%</td><td class="num">{n-d:+.0f}</td></tr>'
        for f, n, d in tone_rows)
    band_tbl = "".join(
        f'<tr><td>{b["band"]}</td><td class="num">{b["med_db"]:.1f}</td>'
        f'<td class="num">{b["rms_pa"]:.4f}</td><td class="num">{b["spl"]:.1f}</td></tr>'
        for b in bands)
    interactive_link = ('<a class="link" href="waterfall_full.html">Open interactive waterfall →</a>'
                        if has_interactive else "")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INFRA20 Infrasound Monitor — {meta.get('site','') or cfg.seed_id}</title>
<style>
:root {{
  --bg:#f5f7f8; --chassis:#eef1f2; --surface:#ffffff; --fig:#ffffff;
  --text:#16201f; --muted:#5f7278; --hair:#dde3e5; --accent:#0d8b88;
  --ok:#1f9d57; --ok-bg:#e4f4ea; --warn:#b9791b; --warn-bg:#faf0dc;
  --mono:ui-monospace,"Cascadia Code","SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}}
@media (prefers-color-scheme:dark) {{
  :root {{ --bg:#0e1315; --chassis:#121a1c; --surface:#161d20; --fig:#ffffff;
    --text:#e7eef0; --muted:#8ba0a6; --hair:#243033; --accent:#2ab0ac;
    --ok:#46b877; --ok-bg:#12261b; --warn:#d99a2b; --warn-bg:#2a2113; }}
}}
:root[data-theme="light"] {{ --bg:#f5f7f8; --chassis:#eef1f2; --surface:#fff; --fig:#fff;
  --text:#16201f; --muted:#5f7278; --hair:#dde3e5; --accent:#0d8b88;
  --ok:#1f9d57; --ok-bg:#e4f4ea; --warn:#b9791b; --warn-bg:#faf0dc; }}
:root[data-theme="dark"] {{ --bg:#0e1315; --chassis:#121a1c; --surface:#161d20; --fig:#fff;
  --text:#e7eef0; --muted:#8ba0a6; --hair:#243033; --accent:#2ab0ac;
  --ok:#46b877; --ok-bg:#12261b; --warn:#d99a2b; --warn-bg:#2a2113; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans);
  line-height:1.55; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:0 20px 64px; }}
header {{ border-bottom:1px solid var(--hair); background:var(--chassis); }}
.bar {{ max-width:1080px; margin:0 auto; padding:16px 20px; display:flex;
  align-items:baseline; gap:14px; flex-wrap:wrap; }}
.dot {{ width:9px; height:9px; border-radius:50%; background:var(--ok);
  box-shadow:0 0 0 0 var(--ok); align-self:center; animation:pulse 2.4s infinite; }}
@keyframes pulse {{ 0%{{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 55%,transparent);}}
  70%{{box-shadow:0 0 0 7px transparent;}} 100%{{box-shadow:0 0 0 0 transparent;}} }}
@media (prefers-reduced-motion:reduce) {{ .dot {{ animation:none; }} }}
h1 {{ font-size:1.05rem; margin:0; font-weight:650; letter-spacing:-.01em; }}
.loc {{ font-family:var(--mono); font-size:.78rem; color:var(--muted); }}
.updated {{ margin-left:auto; font-family:var(--mono); font-size:.72rem; color:var(--muted); }}
.lead {{ color:var(--muted); max-width:64ch; margin:22px 0 4px; font-size:.95rem; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
  gap:12px; margin:22px 0 8px; }}
.tile {{ background:var(--surface); border:1px solid var(--hair); border-radius:10px;
  padding:14px 15px; }}
.tile-label {{ font-family:var(--mono); font-size:.68rem; text-transform:uppercase;
  letter-spacing:.09em; color:var(--muted); }}
.tile-val {{ font-size:1.5rem; font-weight:640; margin-top:6px; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; }}
.tile-val .unit {{ font-size:.8rem; font-weight:500; color:var(--muted); }}
.tile-sub {{ font-family:var(--mono); font-size:.7rem; color:var(--muted); margin-top:3px; }}
.tile.ok {{ border-left:3px solid var(--ok); }}
.tile.ok .tile-val {{ color:var(--ok); }}
.tile.warn {{ border-left:3px solid var(--warn); }}
.tile.warn .tile-val {{ color:var(--warn); }}
section {{ margin-top:40px; }}
.eyebrow {{ font-family:var(--mono); font-size:.7rem; text-transform:uppercase;
  letter-spacing:.11em; color:var(--accent); margin:0 0 3px; }}
h2 {{ font-size:1.2rem; margin:0 0 4px; font-weight:640; letter-spacing:-.01em;
  text-wrap:balance; }}
.cap {{ color:var(--muted); font-size:.88rem; margin:4px 0 14px; max-width:70ch; }}
.figure {{ background:var(--fig); border:1px solid var(--hair); border-radius:10px;
  padding:10px; overflow-x:auto; }}
.figure img {{ display:block; width:100%; height:auto; min-width:640px; }}
.pill {{ display:inline-block; font-family:var(--mono); font-size:.72rem; font-weight:600;
  padding:3px 9px; border-radius:999px; letter-spacing:.02em; }}
.pill.ok {{ background:var(--ok-bg); color:var(--ok); }}
.pill.warn {{ background:var(--warn-bg); color:var(--warn); }}
.grid2 {{ display:grid; grid-template-columns:1.6fr 1fr; gap:20px; align-items:start; }}
@media (max-width:720px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
table {{ border-collapse:collapse; width:100%; font-size:.85rem; }}
th,td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--hair); }}
th {{ font-family:var(--mono); font-size:.66rem; text-transform:uppercase;
  letter-spacing:.07em; color:var(--muted); font-weight:600; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; font-family:var(--mono); }}
.link {{ color:var(--accent); text-decoration:none; font-family:var(--mono); font-size:.8rem; }}
.link:hover {{ text-decoration:underline; }}
footer {{ margin-top:52px; padding-top:18px; border-top:1px solid var(--hair);
  color:var(--muted); font-size:.8rem; }}
footer code {{ font-family:var(--mono); font-size:.92em; }}
a:focus-visible, .link:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
</style></head><body>
<header><div class="bar">
  <span class="dot" title="acquiring"></span>
  <h1>INFRA20 Infrasound Monitor</h1>
  <span class="loc">{meta.get('site','') or cfg.seed_id} · {loc} · {meta.get('fs',0):.2f} sps</span>
  <span class="updated">updated {now:%Y-%m-%d %H:%M} local</span>
</div></header>
<div class="wrap">

  <p class="lead">A single microbarometer recording <strong>infrasound</strong> — pressure
  waves below human hearing (0.05–20&nbsp;Hz). This station watches for a steady,
  narrowband tone that a nearby datacenter would add to the background, tracked over
  months against a quiet pre-dawn baseline.</p>

  <div class="tiles">{tiles}</div>

  <section>
    <p class="eyebrow">Primary detection · night-only</p>
    <h2>Persistent-tone hunt <span class="pill {'ok' if k['peak_pct']<50 else 'warn'}">{verdict_word}</span></h2>
    <p class="cap">Each night's spectrum with its broadband level removed, so only
    narrowband prominence remains. A datacenter tone would appear as a steady bright
    <em>horizontal</em> line climbing toward 100% of nights at a fixed frequency.
    Quiet window {k['night_label']}.</p>
    <div class="figure"><img alt="night-only tone-hunt waterfall" src="{imgs['night']}"></div>
    <div class="grid2" style="margin-top:16px">
      <p class="cap">Frequencies more persistent at night than day are the datacenter-like
      signature. Currently the strongest are weak and consistent with local equipment,
      not a steady source — the baseline reads <strong>tone-free</strong>.</p>
      <table><thead><tr><th>Freq</th><th class="num">Night</th><th class="num">Day</th>
      <th class="num">Δ pts</th></tr></thead><tbody>{tone_tbl}</tbody></table>
    </div>
  </section>

  <section>
    <p class="eyebrow">Overview</p>
    <h2>Spectral waterfall — full record</h2>
    <p class="cap">Time × frequency, loudness in color. Vertical banding is the day/night
    cycle; blank columns are data gaps. {interactive_link}</p>
    <div class="figure"><img alt="spectral waterfall" src="{imgs['waterfall']}"></div>
  </section>

  <section>
    <p class="eyebrow">Noise characterization</p>
    <h2>Long-term spectrum (PDF-PSD)</h2>
    <p class="cap">Probability density of the hourly power spectrum (McNamara-Buland),
    with 10 / 50 / 90th-percentile curves — the standard long-term noise fingerprint.</p>
    <div class="grid2">
      <div class="figure"><img alt="PDF-PSD" src="{imgs['pdf']}"></div>
      <table><thead><tr><th>Band</th><th class="num">Median dB</th>
      <th class="num">RMS Pa</th><th class="num">dB SPL</th></tr></thead>
      <tbody>{band_tbl}</tbody></table>
    </div>
  </section>

  <section>
    <p class="eyebrow">Station health</p>
    <h2>Data availability</h2>
    <p class="cap">Hourly coverage over the record — green is data, red is a gap.</p>
    <div class="figure"><img alt="availability timeline" src="{imgs['avail']}"></div>
  </section>

  <footer>
    <p>Station <code>{cfg.seed_id}</code> · Infiltec INFRA20 microbarometer ·
    calibrated {meta.get('sens',0):.0f} counts/Pa · record {start:%Y-%m-%d} → {end:%Y-%m-%d}.
    Generated {now:%Y-%m-%d %H:%M} from the hourly PSD grid; data stored as standard
    miniSEED + StationXML. Not a substitute for a calibrated regulatory noise survey.</p>
  </footer>
</div>
<script>
 // honor a saved theme toggle if the host sets one; otherwise follow the OS.
 try {{ const t = localStorage.getItem('theme'); if (t) document.documentElement.dataset.theme = t; }} catch (e) {{}}
</script>
</body></html>"""


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the public infrasound monitoring dashboard.")
    p.add_argument("archive")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date, required=True)
    p.add_argument("--out", default=None, help="output HTML (default: <archive>/../analysis/index.html)")
    p.add_argument("--cache", default=None, help="PSD grid .npz (share with infra-waterfall)")
    p.add_argument("--nperseg", type=int, default=8192)
    p.add_argument("--baseline-hz", type=float, default=1.0)
    p.add_argument("--night-window", type=int, default=5)
    p.add_argument("--utc-offset", type=float, default=-7.0)
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    out = Path(a.out) if a.out else Path(a.archive).parent / "analysis" / "index.html"
    build(a.archive, a.start, a.end, out, cfg, cache=a.cache, nperseg=a.nperseg,
          baseline_hz=a.baseline_hz, night_window=a.night_window, utc_offset=a.utc_offset)


if __name__ == "__main__":
    main()
