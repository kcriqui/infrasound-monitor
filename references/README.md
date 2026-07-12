# References

Background reading for this project. The **PDFs themselves are not committed**
(they're `.gitignore`d — two are third-party copyrighted works), but they live in
this folder locally. Citations and links are below so anyone can find the sources.

- **den Ouden, O. F. C., Assink, J. D., Oudshoorn, C. D., Filippi, D., & Evers, L. G.**
  *The INFRA-EAR: a low-cost mobile multidisciplinary measurement platform for
  monitoring geophysical parameters.* Atmospheric Measurement Techniques (EGU),
  2021. Manuscript `amt-2020-371` — open access.
  Background on low-cost MEMS infrasound sensing, calibration, response, and
  noise-model comparison. (Local: `amt-2020-371-manuscript-version3.pdf`)

- **Hackaday — "Constructing an Infrasound Monitor."**
  Maker-oriented article on building/running an infrasound monitor.
  (Local: `Hackaday Constructing Infrasound Monitor.pdf`)

- **`000275.pdf`** — local reference (encrypted PDF; retained for offline use).

## Related tools and standards used by this project
- [Infiltec INFRA20 / Infrasound@home](https://www.infiltec.com/Infrasound@home/) — the sensor.
- [ObsPy](https://docs.obspy.org/) — miniSEED/StationXML I/O, PPSD, spectrograms.
- [FDSN source identifiers](https://docs.fdsn.org/projects/source-identifiers/) — the `SDF` channel code.
- Bowman et al. (2005), *Ambient infrasound noise*, GRL; Brown et al. (2014),
  *IDC global low/high infrasound noise models*, Pure Appl. Geophys. — reference
  noise models (see `tools/noise_models/`).
