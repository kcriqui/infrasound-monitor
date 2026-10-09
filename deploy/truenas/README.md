# Running the analysis on TrueNAS SCALE

The Raspberry Pi is a good data logger and a bad analysis box. Acquisition needs
almost nothing; the nightly rebuild processes the *whole* record and grows with it.
By October 2026 that job wanted ~1.3 GB on a 905 MB Pi, swapped ~1.2 GB to the SD
card, and took the machine down three nights running — acquisition stayed healthy
throughout, which is the clue that the two jobs belong on different machines.

So: the Pi keeps `infra-acquire`, `statusd` and the TFT. TrueNAS pulls the archive,
rebuilds, and serves. At 32 GB there is roughly a decade of headroom against a job
growing ~2.7 GB/year.

Pulling (rather than having the Pi push) means no credentials live on the Pi, and
the pulled copy doubles as the off-Pi **backup** — on ZFS with snapshots, replacing
the 3.6 GB USB stick that was months from full.

## 1. Dataset

```
/mnt/HDD-pool/Projects/Claude/infra/
  .ssh/        id_infra (-> the Pi), id_site (-> the Pages repo)
  archive/     rsynced from the Pi; this is also the backup
  analysis/    grid_full.npz and other caches
  site/        built dashboard; a clone of the Pages repo if you publish
  logs/        pipeline.log
  config.toml  station identity
```

Change the path in `docker-compose.yml` if you use a different one — it appears twice
under `analysis` and once under `web`.

## 2. Keys

Both generated **on the NAS**, kept in the dataset rather than `/root/.ssh` so they
survive SCALE upgrades and are mountable into the container.

```bash
D=/mnt/HDD-pool/Projects/Claude/infra
mkdir -p $D/.ssh && chmod 700 $D/.ssh
ssh-keygen -t ed25519 -N "" -C "truenas->pi"   -f $D/.ssh/id_infra
ssh-keygen -t ed25519 -N "" -C "truenas->site" -f $D/.ssh/id_site
chmod 600 $D/.ssh/id_infra $D/.ssh/id_site
```

`id_infra.pub` goes on the **Pi**, prefixed with `restrict` (no port forwarding, agent
forwarding, PTY or X11 — rsync still works):

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'restrict ssh-ed25519 AAAA...  truenas->pi' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

`id_site.pub` goes on **GitHub** → the *site* repo → Settings → Deploy keys → Add,
with **Allow write access** ticked. Generate a new one rather than copying the Pi's
`infra_site`, so either machine can be revoked independently.

Verify the pull path from the NAS before going further:

```bash
ssh -i $D/.ssh/id_infra -o IdentitiesOnly=yes infra@100.72.190.123 \
    'ls infrasound-monitor/archive/2026/XX/INFRA/SDF.D | tail -3'
```

## 3. Config

```bash
cp config.example.toml /mnt/HDD-pool/Projects/Claude/infra/config.toml
```

Set the station identity to match the Pi's, and point the archive at the container's
view of the dataset:

```toml
[acquisition]
archive = "/data/archive"
```

Serial port and live-file settings are ignored here — nothing acquires on the NAS.
`config.py` reads `$INFRASOUND_CONFIG`, which the image sets to `/data/config.toml`.

## 4. Seed the grid (once)

`refresh.py` extends an existing cache and refuses to create one, so build it once.
Unlike on the Pi, there is no reason to restrict the range:

```bash
cd /mnt/HDD-pool/Projects/Claude/infra && git clone <this repo> repo && cd repo
docker compose -f deploy/truenas/docker-compose.yml build
docker compose -f deploy/truenas/docker-compose.yml run --rm analysis \
    bash deploy/truenas/pipeline.sh        # pulls the archive first
docker compose -f deploy/truenas/docker-compose.yml run --rm analysis \
    python -m infrasound_monitor.waterfall /data/archive \
      --start 2026-04-09 --end $(date -u +%Y-%m-%d) \
      --cache /data/analysis/grid_full.npz --out /tmp/seed.html
```

> **Worth deciding here, not later:** the sensor moved to a quieter spot around
> 2026-08-04, which puts a step change in the noise floor mid-record. Pooling both
> sides into one PDF-PSD and one set of band statistics mixes two different
> environments. Starting the grid at `--start 2026-08-04` gives a clean baseline for
> the datacenter before/after comparison; starting in April keeps continuity. The raw
> archive is untouched either way, so this is reversible — regenerate with a different
> `--start`.

## 5. Publishing (optional)

To keep the public GitHub Pages site, make `site/` a clone of the Pages repo:

```bash
cd /mnt/HDD-pool/Projects/Claude/infra
GIT_SSH_COMMAND="ssh -i .ssh/id_site -o IdentitiesOnly=yes" \
  git clone git@github.com:<you>/<site-repo>.git site
```

`pipeline.sh` then force-pushes a single amended commit after each rebuild. Without a
clone it just rebuilds locally and nginx serves it — the push is an optional mirror.

Hosting locally and publishing are **not** exclusive, and doing both is worth it: the
NAS view is fast and private over Tailscale, while the Pages URL stays public and
shareable, which matters if the point is eventually to show someone the datacenter
evidence.

## 6. Deploy

Apps → Discover → **Custom App** → *Install via YAML*, and paste `docker-compose.yml`.
Then:

- Local dashboard: `http://<nas-tailscale-name>:8081`
- Logs: `tail -f /mnt/HDD-pool/Projects/Claude/infra/logs/pipeline.log`
- Run now rather than waiting for 06:15: `docker exec infra-analysis bash deploy/truenas/pipeline.sh`

## 7. Retire the Pi's copy of this job

Once a NAS rebuild has published successfully:

```bash
sudo systemctl disable --now infra-dashboard.timer
```

**Do not leave both publishing** — two force-pushes to the same Pages repo overwrite
each other and the site flip-flops between fresh and stale.

Afterwards the Pi needs no outbound credentials at all: delete its deploy key from the
site repo on GitHub, and remove `~/.ssh/infra_site*` and the `github-infra-site` block
from `~/.ssh/config`. Its `--backup` timer is also redundant once the NAS is pulling,
though keeping the USB copy as a third one costs nothing.
