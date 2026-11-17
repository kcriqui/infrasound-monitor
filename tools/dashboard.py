#!/usr/bin/env python3
"""Generate the public monitoring dashboard — one self-contained static HTML page.

Ties the analysis outputs (night tone-hunt, spectral waterfall, PDF-PSD, data
availability) into a single ``index.html`` with all images embedded as base64, so
it opens with no server and can be published as-is to any static host (GitHub
Pages / Netlify / S3).  Regenerate on a schedule to keep it fresh.

    python tools/dashboard.py archive \
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
import compare         # sibling tool
import tonetrack       # sibling tool
from infrasound_monitor.config import (StationConfig, DEFAULT_STATION, UTC_OFFSET_HOURS,
                                        WEATHER_STATIONS, CHANGE_DATE)
from infrasound_monitor.psd import build_cache, load_grid, have_mask
from infrasound_monitor.waterfall import _decimate_time

plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "figure.facecolor": "white"})


def _parse_date(s):
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s}")


# The full-record image is ~1300 x 460 px, so drawing more columns or rows than
# this only feeds matplotlib's resampler.  Reducing first keeps the render's
# memory fixed however long the record grows.
IMG_MAX_COLS = 1600
IMG_MAX_ROWS = 800


def _downsample_freq(freqs, img, max_rows):
    """Average groups of adjacent frequency bins, to <= max_rows.  A plain mean of
    the plotted values -- what imshow's antialiasing did with the full array --
    so the image looks as before; averaging in power instead would brighten
    the noise and change what a faint line looks like."""
    k = int(np.ceil(len(freqs) / max_rows))
    if k <= 1:
        return freqs, img
    n = len(freqs) // k * k                    # drop a ragged top end (< k bins)
    out = np.asarray(img[:, :n], float).reshape(len(img), -1, k).mean(axis=2)
    return freqs[:n].reshape(-1, k).mean(axis=1), out


def _fig_waterfall(times, freqs, psd):
    times, psd = _decimate_time(times, psd, IMG_MAX_COLS)
    freqs, psd = _downsample_freq(freqs, psd, IMG_MAX_ROWS)
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
    day_dt, night_wf = _decimate_time(np.array(day_dt, dtype=object), night_wf, IMG_MAX_COLS)
    freqs, night_wf = _downsample_freq(freqs, night_wf, IMG_MAX_ROWS)
    xnum = mdates.date2num(list(day_dt))
    fig, ax = plt.subplots(figsize=(12, 4.6))
    im = ax.imshow(night_wf.T, origin="lower", aspect="auto",
                   extent=[xnum[0], xnum[-1], float(freqs.min()), float(freqs.max())],
                   vmin=0, vmax=6, cmap="magma")
    ax.xaxis_date(); ax.set_ylabel("Frequency (Hz)")
    plt.colorbar(im, ax=ax, label="dB above background")
    fig.autofmt_xdate(); fig.tight_layout()
    return report._fig_b64(fig)


def build(archive, start, end, out_html, cfg, cache=None, nperseg=8192,
          baseline_hz=1.0, night_window=5, utc_offset=UTC_OFFSET_HOURS):
    # Everything below works a block of hours at a time on the (memory-mapped)
    # grid, so this page's memory does not grow with the record.
    if cache and not Path(cache).exists():
        build_cache(cache, archive, start, end, cfg, nperseg=nperseg)
    grid = (load_grid(cache) if cache
            else report.compute_grid(archive, start, end, cfg, nperseg=nperseg))
    times = list(grid["times"]); freqs = np.asarray(grid["freqs"], float)
    psd = grid["psd_db"]
    have = have_mask(psd)

    # night analysis
    thr = 6.0
    night_hours, _ = tonehunt.quiet_window(times, freqs, psd, have, night_window)
    night_wf, day_dt, nmask, pn, pd_ = tonehunt.night_day_stats(
        times, freqs, psd, have, night_hours, baseline_hz, thr)
    loc = [int((h + utc_offset) % 24) for h in night_hours]
    night_label = (f"{night_hours[0]:02d}:00–{(night_hours[-1]+1)%24:02d}:00 UTC "
                   f"· ~{min(loc):02d}:00–{(max(loc)+1)%24:02d}:00 local")
    tones = _tone_tracker(grid)

    av = report.availability(times, have)
    pdf = report.pdf_psd(freqs, psd, have)
    bands = report.band_stats(freqs, med=pdf["p50"])
    meta = _station_meta(archive)

    imgs = dict(waterfall=_fig_waterfall(times, freqs, psd),
                night=_fig_nightly(day_dt, freqs, night_wf, night_label),
                pdf=pdf["img"], avail=av["img"])
    kpis = dict(uptime=av["uptime"], ngaps=av["ngaps"], nights=len(day_dt),
                span_days=(end - start).days, tones=tones,
                night_label=night_label,
                overall=next((b for b in bands if b["band"].startswith("0.05–20")), None))
    cres = _compare(grid)
    kpis["g"] = cres.get("g") if cres else None
    change_html = _change_section(cres, night_label)
    html = _render(cfg, start, end, meta, kpis, bands, imgs,
                   has_interactive=(Path(out_html).parent / "waterfall_full.html").exists(),
                   change_html=change_html)
    Path(out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(out_html).write_text(html, encoding="utf-8")
    print(f"wrote {out_html}")
    print(f"  uptime {kpis['uptime']:.1f}%  nights {kpis['nights']}  "
          f"tones: {tones['verdict'] if tones else 'tracker skipped'}")
    return out_html


def _tone_tracker(grid):
    """Tone-tracker results plus its figure and table, or None (never fails the page)."""
    try:
        res = tonetrack.analyse(grid, dt.datetime.fromisoformat(CHANGE_DATE) if CHANGE_DATE else None)
    except Exception as e:
        print(f"  tone tracker skipped: {e}")
        return None
    res["img"] = report._fig_b64(tonetrack.figure(res))
    rows = []
    for s in res["sources"][:10]:
        label = s["known"] or (f"harmonic, {s['harmonic_of']}" if s["harmonic_of"] else "")
        if s in res["flag"]:
            label = "<strong>unexplained, steady</strong>" + (f" ({label})" if label else "")
        rows.append(f'<tr><td class="num">{s["f"]:.2f}</td>'
                    f'<td class="num">{s["f_lo"]:.2f}–{s["f_hi"]:.2f}</td>'
                    f'<td class="num">{s["occ_base"]*100:.0f}%</td>'
                    f'<td class="num">{s["occ_recent"]*100:.0f}%</td>'
                    f'<td>{tonetrack.schedule(s)}</td><td>{label}</td></tr>')
    res["table"] = "".join(rows)
    return res


def _compare(grid):
    """The before/after analysis (with the G-weighted levels), or None.
    Never fails the page: the rest of the dashboard is worth publishing anyway."""
    if not WEATHER_STATIONS:
        return None
    try:
        return compare.analyse(grid, dt.datetime.fromisoformat(CHANGE_DATE) if CHANGE_DATE else None)
    except Exception as e:                       # bad weather file, too little data, ...
        print(f"  change monitor skipped: {e}")
        return None


def _g_html(res):
    """The G-weighted level subsection: absolute levels against the guideline."""
    g = res.get("g")
    if not g or not g["days"]:
        return ""
    img = report._fig_b64(compare.g_figure(res))
    rows = []
    for key, name in (("before_night", "Earlier, night"), ("after_night", "Later, night"),
                      ("before_all", "Earlier, all hours"), ("after_all", "Later, all hours")):
        s = g[key]
        if s:
            rows.append(f'<tr><td>{name}</td><td class="num">{s["med"]:.1f}</td>'
                        f'<td class="num">{s["p95"]:.1f}</td><td class="num">{s["max"]:.1f}</td>'
                        f'<td>{s["t_max"]:%Y-%m-%d %H:00} UTC</td></tr>')
    return f"""<h2 style="margin-top:28px">G-weighted level</h2>
    <p class="cap">Infrasound weighted by how people perceive it (ISO 7196 G-weighting, in
    dB(G)), the measure that guidelines use: Denmark recommends at most {g['guide']:.0f} dB(G)
    indoors, and ISO 7196 notes that levels below about 90 dB(G) are not normally significant
    for perception. These are absolute levels, not wind-matched; the "G-weighted" row in the
    table above gives the wind-matched change.</p>
    <details class="explain"><summary>Limits of this number</summary>
    <p>G-weighting peaks at 20 Hz, exactly where this sensor's anti-alias filter cuts off,
    so energy at 20–50 Hz that the weighting would count is not measured: read these as lower
    bounds. The sensor's own noise sets a floor (the dotted line, the quietest hour on record),
    and quiet nights sit close to it. The calibration is good to about ±20% (±1.6 dB).</p></details>
    <div class="figure"><img alt="G-weighted level per day" src="{img}"></div>
    <table class="below"><thead><tr><th>Hours</th><th class="num">Median</th>
    <th class="num">95th pct</th><th class="num">Loudest</th><th>Loudest hour</th></tr></thead>
    <tbody>{"".join(rows)}</tbody></table>"""


def _change_section(res, night_label):
    """The wind-matched before/after section, or a note saying what it needs."""
    head = '<section>\n    <p class="eyebrow">Change monitor · wind-matched</p>\n'
    if not WEATHER_STATIONS:
        return (head + '    <h2>Before / after</h2>\n    <p class="cap">Needs a wind record: set '
                '<code>[weather] stations</code> in config.toml (see README).</p>\n  </section>')
    if res is None:
        return ""
    if not res["n_after"] or not res["n_before"]:
        return (head + '    <h2>Before / after</h2>\n    <p class="cap">Not enough hours with '
                'weather data on both sides of the split yet.</p>\n  </section>')
    if res["mode"] == "recent":
        title = f"Last {res['recent_days']} days against the baseline"
        lead = ("No change date is set yet, so this compares the most recent days with "
                "everything before them: a running check that the background is stable.")
    else:
        title = f"Since {res['split']:%Y-%m-%d} against before"
        lead = f"Hours from {res['split']:%Y-%m-%d} (UTC) on are compared with all earlier hours."
    rows = "".join(
        f'<tr><td>{lab}</td><td class="num">{res["delta"][j]:+.1f}</td>'
        f'<td class="num">{res["lo"][j]:+.1f} … {res["hi"][j]:+.1f}</td>'
        f'<td class="num">{res["raw"][j]:+.1f}</td></tr>'
        for j, lab in enumerate(res["labels"]))
    stations = ", ".join(res["stations"])
    img = report._fig_b64(compare.figure(res))
    return f"""{head}    <h2>{title}</h2>
    <p class="cap">{lead} Wind is the main cause of level changes here, so each octave band
    is compared at equal wind: the earlier night hours ({night_label}) give the level the
    baseline had at each wind speed (mean wind at {stations}), and each later hour is
    measured against that at its own wind. Rainy hours are left out.</p>
    <details class="explain"><summary>How to read this</summary>
    <p>A new steady source would show as a positive change whose 90% interval stays above
    zero, in the same bands, as more nights accumulate. The grey rings are the raw change
    without wind matching; the gap between ring and dot is how much of the raw change was
    weather. Seasonal sources still show: ocean microbaroms grow from September, which lifts
    the lowest bands. The interval comes from resampling whole nights.
    {res['n_after']} later and {res['n_before']} earlier night hours,
    {res['coverage']*100:.0f}% of the later ones within the baseline's range of wind.</p></details>
    <div class="figure"><img alt="wind-matched change per band" src="{img}"></div>
    <table class="below"><thead><tr><th>Band</th><th class="num">Change dB</th>
    <th class="num">90% interval</th><th class="num">Raw dB</th></tr></thead>
    <tbody>{rows}</tbody></table>
    {_g_html(res)}
  </section>"""


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


def _g_tile(g):
    """Recent night median G-weighted level, with the loudest recent hour."""
    s = g and (g["after_night"] or g["before_night"])
    if not s:
        return _tile("Infrasound, G-wtd", "—", "needs weather data")
    loud = g["after_all"] or g["before_all"]
    state = "warn" if loud["max"] >= g["guide"] else "ok"
    return _tile("Infrasound, G-wtd", f'{s["med"]:.0f}<span class="unit"> dB(G)</span>',
                 f'night median · loudest hour {loud["max"]:.0f} · guideline {g["guide"]:.0f}',
                 state)


def _render(cfg, start, end, meta, k, bands, imgs, has_interactive, change_html=""):
    now = dt.datetime.now()
    tones = k["tones"]
    if tones is None:
        verdict_state, verdict_word, verdict_sub = "", "—", "tracker unavailable"
    elif tones["flag"]:
        verdict_state, verdict_word = "warn", "Unexplained tone"
        verdict_sub = ", ".join(f"{s['f']:.2f} Hz" for s in tones["flag"])
    else:
        verdict_state, verdict_word = "ok", "None unexplained"
        verdict_sub = f"{len(tones['sources'])} tracked sources"
    loc = (f'{meta.get("lat"):.4f}, {meta.get("lon"):.4f}'
           if meta.get("lat") is not None else "—")
    ov = k["overall"]
    noise_spl = f'{ov["spl"]:.0f} dB SPL' if ov else "—"

    tiles = "".join([
        _tile("Data uptime", f'{k["uptime"]:.1f}<span class="unit">%</span>',
              f'{k["ngaps"]} gaps', "ok" if k["uptime"] >= 85 else "warn"),
        _tile("Steady tone", verdict_word, verdict_sub, verdict_state),
        _tile("Ambient level", noise_spl.split()[0] + '<span class="unit"> dB SPL</span>',
              "median, 0.05–20 Hz"),
        _g_tile(k.get("g")),
        _tile("Coverage", f'{k["span_days"]}<span class="unit"> days</span>',
              f'{k["nights"]} nights analyzed'),
        _tile("Quiet window", k["night_label"].split("·")[1].strip(),
              "auto-detected (highest-SNR)"),
    ])

    if tones is not None:
        recent_txt = (f"since {tones['split']:%Y-%m-%d}" if CHANGE_DATE
                      else f"last {(now - tones['split']).days + 1} days")
        tracker_html = f"""<h2 style="margin-top:28px">Tracked tones
    <span class="pill {verdict_state}">{verdict_word}</span></h2>
    <p class="cap">Every hour's narrowband peaks, linked hour to hour into tracks that follow a
    tone as its frequency drifts, then grouped into sources. A datacenter would show as an
    <em>unexplained steady</em> source: present in most hours, around the clock, in the recent
    window ({recent_txt}). Known local sources and harmonics are labelled; a known one is
    flagged again if it turns steady.</p>
    <div class="figure"><img alt="tracked tones" src="{tones['img']}"></div>
    <table class="below" style="max-width:none"><thead><tr><th class="num">Hz</th>
    <th class="num">Range</th><th class="num">Baseline</th><th class="num">Recent</th>
    <th>Schedule (local)</th><th>Label</th></tr></thead><tbody>{tones['table']}</tbody></table>"""
    else:
        tracker_html = ""
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
.explain {{ margin:-6px 0 14px; }}
.explain summary {{ font-family:var(--mono); font-size:.76rem; color:var(--accent);
  cursor:pointer; width:fit-content; letter-spacing:.02em; }}
.explain summary:hover {{ text-decoration:underline; }}
.explain summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
.explain p {{ color:var(--muted); font-size:.86rem; max-width:70ch; margin:8px 0 0; }}
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
table.below {{ width:auto; min-width:min(560px,100%); max-width:560px; margin-top:14px; }}
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
    <h2>Persistent-tone hunt</h2>
    <p class="cap">Each night's spectrum with its broadband level removed, so only
    narrowband prominence remains. A datacenter tone would appear as a steady bright
    <em>horizontal</em> line at a fixed frequency. Quiet window {k['night_label']}.</p>
    <div class="figure"><img alt="night-only tone-hunt waterfall" src="{imgs['night']}"></div>
    {tracker_html}
  </section>

  {change_html}

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
    <p class="cap"><strong>PDF-PSD — the probability density function of the power spectral
    density.</strong> A noise fingerprint of the whole record: for each frequency, color shows how
    often (%) each loudness level occurred — bright = the typical level, dark = rare. The three
    curves are the quiet (10th&nbsp;pct), typical (median), and loud (90th&nbsp;pct) hourly levels.</p>
    <details class="explain"><summary>How to read this</summary>
    <p>Each vertical slice is a histogram of the ~2,000 hourly spectra at that frequency. It's a
    "red" spectrum — loudest at low frequency (wind, weather, microbaroms) and quieter toward
    20&nbsp;Hz. The wide spread at low frequency is the day/night swing; the tight band up high is
    the sensor's steady noise floor. A datacenter tone would appear as a bright pip at one fixed
    frequency <em>and</em> lift the quiet (10th-pct) line there — because even the calmest hours
    would then carry the tone.</p></details>
    <div class="figure"><img alt="PDF-PSD" src="{imgs['pdf']}"></div>
    <table class="below"><thead><tr><th>Band</th><th class="num">Median dB</th>
    <th class="num">RMS Pa</th><th class="num">dB SPL</th></tr></thead>
    <tbody>{band_tbl}</tbody></table>
  </section>

  <section>
    <p class="eyebrow">Station health</p>
    <h2>Data availability</h2>
    <p class="cap">Hourly coverage over the record — green is data, red is a gap.</p>
    <div class="figure"><img alt="availability timeline" src="{imgs['avail']}"></div>
  </section>

  <footer>
    <p>Station <code>{cfg.seed_id}</code> ·
    <a class="link" href="https://www.infiltec.com/Infrasound@home/">Infiltec INFRA20 microbarometer</a> ·
    calibrated {meta.get('sens',0):.0f} counts/Pa · record {start:%Y-%m-%d} → {end:%Y-%m-%d}.
    Generated {now:%Y-%m-%d %H:%M} from the hourly PSD grid; data stored as standard
    miniSEED + StationXML. Not a substitute for a calibrated regulatory noise survey.</p>
    <p><a class="link" href="https://github.com/kcriqui/infrasound-monitor">Open source
    — code, tools, and deployment guide on GitHub →</a></p>
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
    p.add_argument("--utc-offset", type=float, default=UTC_OFFSET_HOURS)
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
