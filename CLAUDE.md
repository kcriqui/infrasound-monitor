# infrasound-monitor

Records an Infiltec INFRA20 infrasound sensor to standard miniSEED + StationXML and
builds a nightly spectral dashboard. Linux / Raspberry Pi only (systemd).

## Where to look

| Need | Read |
|---|---|
| What the project does, CLI usage, code layout | [README.md](README.md) |
| Deploying on a Pi: setup flags, serving, cutover, SSD boot | [DEPLOY.md](DEPLOY.md) |
| The optional TrueNAS backup/analysis stack | [deploy/truenas/README.md](deploy/truenas/README.md) |
| Station settings (port, coordinates, sample rate) | [config.example.toml](config.example.toml) |
| Grid cache format (memory-mapped `.npz` + `.psd.f32`) | `src/infrasound_monitor/psd.py` docstrings |

## Working rules

- **Don't reinvent formats.** Data stays miniSEED (raw int counts) + StationXML so it
  works with ObsPy/Swarm/FDSN tools; calibration lives in the metadata.
- **Keep the nightly rebuild's memory flat.** It must run on a 1 GB Pi: stream over the
  memory-mapped grid with `psd.iter_blocks`; never materialise the whole grid as float64.
- **Only one machine builds the dashboard.**
- **Never expose `tools/statusd.py` (port 8080) publicly**; it has no authentication.
- Tests: `python -m pytest -q tests` (needs the package's deps: numpy, scipy, obspy).
- Shell scripts and systemd units are LF (`.gitattributes`).
