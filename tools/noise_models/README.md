# External infrasound noise-model overlay

`tools/analyze.py --noise-model <file.csv>` overlays published **global infrasound
noise models** on the station PPSD, so you can see where this station's noise sits
relative to the worldwide low/high envelope.

**No curve values are bundled here** — the published models (below) are distributed
as figures / paywalled papers, not as a clean table, so they must be digitized and
filled in by hand rather than guessed. Copy `infrasound_global.TEMPLATE.csv`, add
rows, and pass it with `--noise-model`.

## CSV format

```
freq_hz,low_db,high_db,median_db
0.02,...,...,...
```

- `freq_hz` — frequency in Hz (roughly 0.02–7 Hz for the classic models).
- `low_db` / `high_db` / `median_db` — power spectral density in **dB relative to
  1 Pa²/Hz**. This MUST match the analyzer's PPSD axis (which plots
  `10·log10(PSD in Pa²/Hz)` via `special_handling="hydrophone"`). `median_db` is
  optional; include whichever columns you have.
- Lines starting with `#` are comments.

**Units caveat when digitizing:** some papers plot **PASD** (pressure *amplitude*
spectral density, Pa/√Hz) or use a dB reference of 20 µPa. Convert to PSD dB re
1 Pa²/Hz before entering: `PSD_dB = 20·log10(PASD)`; if a source is in dB re 20 µPa,
subtract `20·log10(20e-6) = -94 dB` appropriately. Getting the reference right is
what makes the overlay line up with the PPSD.

## Sources to digitize from

- **Bowman, Baker & Bahavar (2005)**, *Ambient infrasound noise*, Geophys. Res.
  Lett. 32, L09803 — low/median/high global models, 0.03–7 Hz (their Fig. 2).
- **Brown, Ceranna, Prior, Mialle & Le Bras (2014)**, *The IDC Seismic,
  Hydroacoustic and Infrasound Global Low and High Noise Models*, Pure Appl.
  Geophys. 171:361–375 — the IDC infrasound low/high models.

If you obtain either paper's digitized curve (or a data file from the authors/CTBTO),
drop the numbers into a CSV here and the overlay is automatic.
