# Archive backup (and optional analysis) on TrueNAS SCALE

Each morning at 06:15 this stack pulls the Raspberry Pi's miniSEED archive onto a
TrueNAS dataset. ZFS snapshots make that pulled copy the station's off-Pi **backup**,
replacing the small USB stick as the copy that matters. Pulling (rather than having the
Pi push) means the Pi holds no credentials for the NAS.

**Current mode: pull-only.** The Pi rebuilds the dashboard itself, peaking at ~300 MB
on a 1 GB Pi 3B+, and serves it over Tailscale Funnel (see DEPLOY.md). The compose file
sets `INFRA_PULL_ONLY: "1"`, so the NAS only pulls. The stack can also rebuild and serve
the dashboard here instead; see the last section.

*History:* from Oct 10 to 29, 2026 this box did the nightly rebuild. That was because
the rebuild's memory then grew with the record, and it had outgrown the Pi. The grid
cache is now memory-mapped and the rebuild streams over it, so its memory stays flat,
and the job went back to the Pi.

## 1. Dataset

The dataset root **is** a clone of this repo, with the data beside the code — the same
shape as the Pi's clone, which keeps its `archive/` inside it too:

```
/mnt/HDD-pool/Projects/Claude/Infrasound/
  src/ tools/ deploy/ ...  the code (tracked)
  .ssh/        id_infra (-> the Pi), known_hosts (the Pi's pinned host key)
  archive/     rsynced from the Pi; this is the backup
  logs/        pipeline.log
  config.toml  station identity (only used when rebuilding here)
```

Everything but the code is git-ignored, and docker-ignored too, so the keys and the
archive never reach the image even though the build context is the dataset root.
Clone first, while the dataset is still empty — git refuses to clone into a non-empty
directory:

```bash
git clone <this repo> /mnt/HDD-pool/Projects/Claude/Infrasound
```

Change the path in `docker-compose.yml` if you use a different one.

## 2. Key

Generated **on the NAS**, kept in the dataset rather than `/root/.ssh` so it survives
SCALE upgrades and can be mounted into the container:

```bash
D=/mnt/HDD-pool/Projects/Claude/Infrasound
mkdir -p $D/.ssh
ssh-keygen -t ed25519 -N "" -C "truenas->pi" -f $D/.ssh/id_infra
sudo chown 3000:0 $D/.ssh/id_infra    # 3000 = kevin, the dataset owner the container runs as
```

`id_infra.pub` goes on the **Pi**, prefixed with `restrict` (no port forwarding, agent
forwarding, PTY or X11 — rsync still works):

```bash
echo 'restrict ssh-ed25519 AAAA...  truenas->pi' >> ~/.ssh/authorized_keys
```

Pin the Pi's host key in `$D/.ssh/known_hosts`. The pull uses
`StrictHostKeyChecking=yes`, so a changed host key stops it rather than being accepted.

### A note on permissions

Don't try to `chmod 600` the key. The dataset is an SMB share with NFSv4 ACLs and
`aclmode=restricted`, so POSIX `chmod` is refused **even for root** -- the ACL is
authoritative. `chown` works; `chmod` returns `Operation not permitted`.

`pipeline.sh` handles this. At the start of every run it copies the key to `/tmp/ssh`
inside the container with mode 600, and uses the copy. The overlay filesystem has
ordinary POSIX semantics, so this sidesteps the host ACLs. The key must stay readable by
the container's uid. That makes it readable through the SMB share too; if that matters,
keep `.ssh/` on a separate, unshared dataset owned by the same uid.

To test the pull **by hand**, mirror what the container does, or ssh will reject the
key's mode:

```bash
cd /mnt/HDD-pool/Projects/Claude/Infrasound
sudo install -m 600 .ssh/id_infra /tmp/k
sudo ssh -i /tmp/k -o IdentitiesOnly=yes -o UserKnownHostsFile=.ssh/known_hosts \
    infra@100.72.190.123 'ls infrasound-monitor/archive/2026/XX/INFRA/SDF.D | tail -3'
sudo rm -f /tmp/k
```

## 3. Deploy

Run it with the Compose CLI from the dataset root, **not** as a TrueNAS *Custom App*.
The compose file uses relative paths (`build.context: ../..` and `./nginx.conf`), which
only resolve from the repo. A pasted Custom App YAML has no repo beside it, and it would
be renamed `ix-<app>` as well.

```bash
cd /mnt/HDD-pool/Projects/Claude/Infrasound
docker compose -f deploy/truenas/docker-compose.yml build
docker compose -f deploy/truenas/docker-compose.yml up -d
```

The project is named **`infrasound`**, pinned by `name:` in the compose file rather
than taken from the directory name (which would make it `truenas`). So
`docker compose ls` lists it as `infrasound`, its network is `infrasound_default`, and
`docker compose -p infrasound ps` works from any directory. The container uses
`restart: unless-stopped`, so it comes back after a NAS reboot.

- Logs: `tail -f /mnt/HDD-pool/Projects/Claude/Infrasound/logs/pipeline.log`. A good
  pull-only run ends `[done] pull-only -- no rebuild`. `rc=23 ... chmod refused by the
  dataset ACL` is expected and forgiven: rsync always tries to set modes, and the ACL
  refuses.
- Run now rather than waiting for 06:15: `docker exec infra-analysis bash deploy/truenas/pipeline.sh`

> **After changing any file in the repo, rebuild before running.** The image
> `COPY`s the source, so a `git pull` alone changes nothing the container sees --
> it will keep running the old script and the symptom is a stale log message
> rather than an error:
>
> ```bash
> docker compose -f deploy/truenas/docker-compose.yml build \
>   && docker compose -f deploy/truenas/docker-compose.yml up -d
> ```

## Optional: rebuild and serve the dashboard here instead

Only one machine should build the dashboard. Otherwise one of the two sites silently
goes stale. To move the rebuild onto the NAS:

1. On the Pi: `sudo systemctl disable --now infra-dashboard.timer` and
   `sudo tailscale funnel --https=443 off`.
2. Copy `config.toml` here from `config.example.toml`, with the Pi's station identity
   and `archive = "/data/archive"`. `config.py` reads `$INFRASOUND_CONFIG`, which the
   image sets to `/data/config.toml`.
3. Copy the Pi's grid cache (`analysis/grid_full.npz` + `grid_full.psd.f32`) into
   `analysis/` here. Or seed a new one, **starting at 2026-08-04**, the date the sensor
   moved to a quieter spot (pooling both sides would mix two acoustic environments):

   ```bash
   docker compose -f deploy/truenas/docker-compose.yml run --rm analysis \
       python -m infrasound_monitor.waterfall /data/archive \
         --start 2026-08-04 --end $(date -u +%Y-%m-%d) \
         --cache /data/analysis/grid_full.npz --out /tmp/seed.html
   ```

4. Set `INFRA_PULL_ONLY: "0"`, rebuild, and start with the web server:
   `docker compose -f deploy/truenas/docker-compose.yml --profile serve up -d`. nginx
   serves `site/` on `127.0.0.1:8081`.
5. Expose it with Tailscale. The NAS's own 443 already funnels bartendro, and Funnel
   only listens on 443, 8443 and 10000, so use another port:
   `tailscale funnel --bg --https=8443 http://127.0.0.1:8081`. Tailscale runs as the
   `ix-tailscale-tailscale-1` app container, so prefix with
   `docker exec ix-tailscale-tailscale-1`. Point the README's dashboard link at the new URL.

> **Never funnel the Pi's `statusd` (port 8080).** It has no authentication and
> exposes host telemetry. It is fine on the tailnet; it is not fine on the internet.
