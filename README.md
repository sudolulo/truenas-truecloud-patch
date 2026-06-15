# truenas-truecloud-patch

Extends TrueNAS SCALE's **TrueCloud Backup** feature to work with S3-compatible
providers and native Backblaze B2, instead of Storj only.

---

## Why this exists

In 2026, Storj raised the price of their TrueNAS-integrated storage tier from
**$5/month to $50/month** — a 10× increase. For many home lab and small-office
users, the TrueCloud Backup feature became unaffordable overnight.

TrueCloud Backup is the only native TrueNAS mechanism that provides:
- Integrated ZFS snapshot support before each backup
- Restic-based incremental deduplication
- Scheduled tasks with progress and log tracking in the UI
- Dataset lock integration

Running restic manually is possible but loses all of the above.
This patch restores access to the TrueCloud Backup feature for users who
need a provider other than Storj, with storage they already pay for or that
costs a fraction of the new Storj price.

---

## ⚠ Disclaimer — please read before installing

**This project is unofficial, unsupported, and not affiliated with iXsystems
or the TrueNAS project in any way.**

By installing this patch you accept the following:

- **Unsupported configuration.** TrueNAS support staff are not obligated to
  help with any issue on a system running this patch. If you file a bug report,
  remove the patch first and reproduce the issue on an unmodified system.

- **May break on TrueNAS updates.** The patch targets internal middleware APIs
  that are not part of any public contract. They can change at any time. When
  they do, the patch silently degrades to Storj-only behaviour rather than
  breaking TrueNAS — but you should check the log after each update.

- **Your backups are your responsibility.** Verify that your backup jobs
  complete successfully and that restores work before relying on them for
  disaster recovery.

- **No warranty.** This software is provided as-is. See the LICENSE file.

If TrueNAS ever adds native support for additional providers in TrueCloud
Backup, uninstall this patch immediately.

---

## What is actually patched

**Nothing in TrueNAS's persistent database or configuration is modified.**
The patch operates on two files that live in `/usr/` (which TrueNAS replaces
on every update) and are therefore re-applied automatically on every boot.

| Layer | What changes | Technique |
|---|---|---|
| **Backend** | `B2RcloneRemote` gains `get_restic_config()`. `restic.py` URL builder is fixed for providers with no hostname component (`b2:bucket/path` vs the broken `b2:/bucket/path`). | `sitecustomize.py` — Python's standard startup hook; no middleware files are modified on disk |
| **UI** | The Angular bundle's `filterByProviders` binding is widened from `["STORJ_IX"]` to `["STORJ_IX","S3","B2"]` | In-place text replacement in the compiled JS bundle; original is backed up |

Both changes are **fail-safe**: if a patch cannot be applied (e.g. TrueNAS
restructured the relevant code), middlewared starts normally with Storj-only
support and the reason is logged to `/data/truecloud-patch/apply.log`.

## Supported providers after patching

| Provider | Credential type in TrueNAS |
|---|---|
| Backblaze B2 (native B2 API) | `B2` |
| AWS S3, Wasabi, Cloudflare R2, MinIO, and any S3-compatible endpoint | `S3` |
| Storj (unchanged) | `STORJ_IX` |

## How persistence works

TrueNAS SCALE updates replace `/usr/` entirely. The patch survives by storing
all scripts in `/data/truecloud-patch/` (a persistent ZFS dataset) and
registering a **PREINIT initshutdownscript** in the TrueNAS database. This
causes `apply.sh` to run on every boot before `middlewared` starts, placing
`sitecustomize.py` in the correct site-packages directory and re-patching the
UI bundle.

## Python version compatibility

`sitecustomize.py` uses the `find_spec` / `exec_module` import hook API
(introduced in Python 3.4, required from Python 3.12 onwards). This covers:

- TrueNAS SCALE 24.x — Debian 12, Python 3.11 ✓
- TrueNAS SCALE 25.x — Debian 13, Python 3.12 ✓

---

## Install

Run on your TrueNAS box as root, with the system fully booted:

```bash
git clone https://github.com/sudolulo/truenas-truecloud-patch.git
cd truenas-truecloud-patch
bash install.sh
```

Refresh your browser. S3 and B2 credentials now appear in the
**Data Protection → TrueCloud Backup → Add** credential dropdown.

## Creating a credential

Before creating a backup task, add a credential in the TrueNAS UI:

**Data Protection → TrueCloud Backup → Add → (create new credential)**

Or use **Credentials → Backup Credentials → Cloud Credentials → Add** and
select B2 or Amazon S3.

For S3-compatible providers, select **Amazon S3**, then set a custom endpoint
in the credential's advanced settings (e.g. `https://s3.wasabisys.com`).

## Creating a task via CLI

If the UI still shows only Storj after refreshing (e.g. the JS bundle pattern
changed in a new TrueNAS version), create tasks directly via the REST API:

```bash
# List your cloud credentials to find the right ID
python3 /data/truecloud-patch/create_task.py \
    --host 192.168.1.1 --api-key <key> list-credentials

# Create a task with a B2 credential (id=3)
python3 /data/truecloud-patch/create_task.py \
    --host 192.168.1.1 --api-key <key> create \
    --name "tank-to-b2" \
    --path /mnt/tank/data \
    --credential 3 \
    --bucket my-bucket \
    --folder backups/tank \
    --password "restic-repo-password" \
    --keep-last 14
```

Get an API key from **System → API Keys → Add**.

---

## Uninstall

```bash
bash /path/to/truenas-truecloud-patch/uninstall.sh
```

Removes the PREINIT hook, `sitecustomize.py`, and restores the original UI
bundle from backup. The backend changes vanish on the next `middlewared`
restart.

---

## After a TrueNAS update

1. Check the log: `cat /data/truecloud-patch/apply.log | tail -30`
2. If you see "WARNING: … pattern not found", the UI patch needs updating.
   [Open an issue](https://github.com/sudolulo/truenas-truecloud-patch/issues)
   with your TrueNAS version number.
3. The backend patch (B2 support + URL fix) is more stable — check that a
   B2 backup job still completes successfully after any update.

---

## Emergency recovery

If middlewared stops starting after installing this patch, run this from the
TrueNAS shell (local console, SSH, or the debug shell in the UI):

```bash
bash /data/truecloud-patch/recover.sh
```

That creates a kill-switch file (`/data/truecloud-patch/disabled`) that
`sitecustomize.py` checks at startup. With the switch set, the import hook is
skipped entirely and middlewared starts clean. Your system returns to
Storj-only TrueCloud Backup — nothing else is affected.

If you cannot run a script and only have a bare shell prompt:

```bash
touch /data/truecloud-patch/disabled
systemctl restart middlewared
```

To re-enable the patch after investigating:

```bash
rm /data/truecloud-patch/disabled
bash /data/truecloud-patch/apply.sh
```

---

## Troubleshooting

**Apply log** (check after each reboot or install):
```bash
cat /data/truecloud-patch/apply.log
```

**Verify backend patch is loaded** (while middlewared is running):
```bash
python3 -c "
from middlewared.rclone.remote.b2 import B2RcloneRemote
print('B2 restic support:', hasattr(B2RcloneRemote, 'get_restic_config'))
"
```

**Verify the UI patch** (should print your TrueNAS version):
```bash
grep -c 'STORJ_IX.*S3.*B2' \
    $(find /usr/share/truenas -name '*.js' 2>/dev/null) 2>/dev/null \
    | grep -v ':0'
```

**B2 backup fails with credential error**
Confirm the credential type is exactly `B2` (not `S3` with a B2 endpoint).
B2's native restic backend uses a different auth path than S3-compatible B2.

**S3-compatible backup fails**
S3 support already existed in the backend — the credential setup is the likely
issue. Verify the endpoint URL, access key, secret key, and bucket name in
the credential settings. Use `--insecure` in create_task.py only if you are
using a self-signed certificate on your S3 endpoint.
