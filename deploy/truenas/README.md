# Archive backup on TrueNAS SCALE

The Raspberry Pi does all the work: it acquires, rebuilds the dashboard and serves it
(see [DEPLOY.md](../../DEPLOY.md)). This folder adds one optional thing: a daily job
that pulls the Pi's miniSEED archive onto a TrueNAS dataset. With ZFS snapshots on that
dataset, this is the station's off-Pi **backup**.

The NAS pulls rather than having the Pi push, so the Pi holds no credentials for the
NAS. There's no container: `pull.sh` runs from a TrueNAS cron job using the host's own
`rsync` and `ssh`.

## 1. Dataset

The dataset root is a clone of this repo, with the data beside the code. Clone first,
while the dataset is still empty, because git refuses to clone into a non-empty
directory:

```bash
git clone <this repo> /mnt/POOL/DATASET
```

It ends up holding, all git-ignored:

```
.ssh/                id_infra (-> the Pi), known_hosts (the Pi's pinned host key)
archive/             rsynced from the Pi; this is the backup
logs/pull.log        one [pull] and one [done] line per good run
deploy/truenas/.env  your settings
```

Enable a periodic snapshot task on the dataset. The pull never deletes, but snapshots
are what protect against a file on the Pi being damaged and then copied over.

## 2. Key

Generate it **on the NAS**, in the dataset so it survives SCALE upgrades:

```bash
cd /mnt/POOL/DATASET
mkdir -p .ssh
ssh-keygen -t ed25519 -N "" -C "truenas->pi" -f .ssh/id_infra
ssh-keyscan -t ed25519 <pi address> > .ssh/known_hosts    # check the fingerprint
chown -R <dataset owner> .ssh
```

Authorize `id_infra.pub` on the **Pi**, limited to reading the archive. `rrsync` ships
with rsync on Debian and Raspberry Pi OS:

```bash
echo 'restrict,command="/usr/bin/rrsync -ro /home/<user>/infrasound-monitor/archive" ssh-ed25519 AAAA... truenas->pi' >> ~/.ssh/authorized_keys
```

The NAS can then copy the archive and nothing else: no shell, no writes, no other paths.
Remote paths are relative to that folder, which is why `.env` below uses `/`.

## 3. Settings

Create `deploy/truenas/.env`:

```bash
INFRA_PI=<user>@<pi address>
INFRA_PI_ARCHIVE=/
```

## 4. Schedule

In **System → Advanced → Cron Jobs**, add a daily job (06:15 here) that runs as the
**dataset's owner**:

```bash
bash /mnt/POOL/DATASET/deploy/truenas/pull.sh
```

Run it once by hand with `sudo -u <dataset owner> bash deploy/truenas/pull.sh`, then
check `logs/pull.log`. A good run ends `[done]`. If it fails, the log says why, ending
`[pull] FAILED`.

## Notes on permissions

These apply when the dataset is an SMB share with NFSv4 ACLs and `aclmode=restricted`:

- **`chmod` is refused even for root**, because the ACL is authoritative. So the key
  can't be mode 600 where it lives, and ssh refuses a group-readable key. `pull.sh`
  copies it to a private temp folder for each run. The key must stay readable by the
  dataset's owner, which makes it readable through the SMB share too; if that matters,
  keep `.ssh/` on a separate, unshared dataset.
- **`rsync rc=23`** can appear when rsync sets a mode on a new file and the ACL refuses.
  The bytes are fine, so `pull.sh` forgives rc 23, but only when every error line is a
  refused chmod. Anything else fails the run.
