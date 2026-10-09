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
**Start at 2026-08-04**, the date the sensor moved to a quieter spot in the house.
That move put a step change in the noise floor, and pooling both sides into one
PDF-PSD and one set of band statistics would mix two different acoustic
environments -- which matters, because the whole point is a clean "before"
against which to judge the datacenter once it energises. Memory no longer forces
this choice; it is the right one on the science.

The pre-move record is not lost: the raw archive is untouched and the grid is
only a cache, so a different epoch is one regeneration away.

```bash
cd /mnt/HDD-pool/Projects/Claude/infra && git clone <this repo> repo && cd repo
docker compose -f deploy/truenas/docker-compose.yml build
docker compose -f deploy/truenas/docker-compose.yml run --rm analysis \
    bash deploy/truenas/pipeline.sh        # pulls the archive first
docker compose -f deploy/truenas/docker-compose.yml run --rm analysis \
    python -m infrasound_monitor.waterfall /data/archive \
      --start 2026-08-04 --end $(date -u +%Y-%m-%d) \
      --cache /data/analysis/grid_full.npz --out /tmp/seed.html
```

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

## 6. A public URL over Tailscale Funnel

You already funnel another project at the root of `https://truenas-scale.tail4c32e5.ts.net/`.
Funnel only listens on **443, 8443 or 10000**, and 443's root is taken, so put the
dashboard on 8443:

```bash
tailscale funnel --bg --https=8443 http://127.0.0.1:8081
tailscale funnel status
```

That gives `https://truenas-scale.tail4c32e5.ts.net:8443/`.

**Why a separate port rather than a path under 443.** The generated pages use only
relative links and inline every asset, so they work fine under a path prefix -- but
only when the URL carries a trailing slash. `.../infra/` resolves
`waterfall_full.html` correctly; `.../infra` resolves it against the site root and
404s. A port sidesteps that whole class of bug. If you prefer the tidier URL:

```bash
tailscale serve --bg --set-path=/infra http://127.0.0.1:8081
```

and always share the trailing-slash form.

Funnel relays through Tailscale's edge, so your home IP is never exposed, and the
`web` container binds to `127.0.0.1` so the only way in is through Tailscale.

> **Do not funnel the Pi's `statusd` (port 8080).** It has no authentication and
> exposes host telemetry. It is fine on the tailnet; it is not fine on the internet.

### Optional: its own tailnet hostname instead of :8443

Funnel's three ports are a hard limit on Tailscale's side -- no flag or plan adds
more. But the limit is per *node*, and every node gets its own MagicDNS name and
its own 443. So a Tailscale sidecar gives this stack:

```
https://infra.<tailnet>.ts.net/
```

Clean root, no port, no trailing-slash caveat, and the pattern repeats for each
future project on this box.

1. **Tailnet ACLs** -- the tag must exist and be allowed to funnel:

   ```json
   "tagOwners": { "tag:infra": ["autogroup:admin"] },
   "nodeAttrs": [ { "target": ["tag:infra"], "attr": ["funnel"] } ]
   ```

2. **Auth key** -- generate a **reusable, pre-approved, tagged** key
   (`tag:infra`) in the admin console. Tagged nodes do not expire, so it will not
   silently drop off in three months.

   Put it in `deploy/truenas/.env` (gitignored -- never commit it):

   ```
   TS_AUTHKEY=tskey-auth-...
   ```

   If you install via the TrueNAS *Custom App* YAML box rather than the CLI,
   substitute the key directly there instead; it is stored in TrueNAS's config,
   not in the repo.

3. **Enable it** -- in `docker-compose.yml`, strip the leading `# ` from every
   line between `BEGIN tailscale-sidecar` and `END tailscale-sidecar`, then on
   the `web` service comment out `ports` and uncomment `network_mode`.

The state volume matters: without `/var/lib/tailscale` persisted, the container
registers as a *new* node on every restart and you accumulate `infra-1`,
`infra-2`... each with a different URL. `TS_USERSPACE=true` avoids needing
`NET_ADMIN` or `/dev/net/tun`.

Then `tailscale funnel status` on the NAS should list the new node, and the old
`--https=8443` funnel can be removed with `tailscale funnel --https=8443 off`.

With Funnel live, GitHub Pages becomes optional. Worth keeping both for a while:
Pages is a CDN-backed mirror that stays up when the NAS or your home connection does
not, and the Pages URL is already in the repo README and may be bookmarked elsewhere.
The pipeline pushes to it anyway, so it costs nothing.

## 7. Deploy

Apps → Discover → **Custom App** → *Install via YAML*, and paste `docker-compose.yml`.
Then:

- Public dashboard: `https://truenas-scale.tail4c32e5.ts.net:8443/` (via Funnel, above)
- From the NAS itself: `http://127.0.0.1:8081`
- Logs: `tail -f /mnt/HDD-pool/Projects/Claude/infra/logs/pipeline.log`
- Run now rather than waiting for 06:15: `docker exec infra-analysis bash deploy/truenas/pipeline.sh`

## 8. Retire the Pi's copy of this job

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
