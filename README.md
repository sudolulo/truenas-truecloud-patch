# truenas-truecloud-patch

Extends TrueNAS SCALE's **TrueCloud Backup** feature to support S3-compatible and native Backblaze B2 providers, instead of Storj only.

TrueCloud Backup already uses [restic](https://restic.net) under the hood.  
This patch just removes the artificial restriction.

---

## What it patches

| Layer | What changes |
|---|---|
| **Backend** | `B2RcloneRemote` gains `get_restic_config()` so native B2 repos work. `restic.py` URL construction is fixed for providers with no hostname component (`b2:bucket/path` instead of the broken `b2:/bucket/path`). |
| **UI** | The Angular bundle's `filterByProviders` binding in the TrueCloud task form is widened from `["STORJ_IX"]` to `["STORJ_IX","S3","B2"]`, so those three credential types appear in the dropdown. |

## Supported providers after patching

| Provider | Credential type | Notes |
|---|---|---|
| Backblaze B2 (native) | `B2` | Requires this patch |
| AWS S3 / Wasabi / Cloudflare R2 / MinIO / etc. | `S3` | Already worked at the API level; UI restriction removed by patch |
| Storj | `STORJ_IX` | Unchanged — still works |

## How persistence works

TrueNAS updates replace `/usr/` entirely. The patch survives by:

1. Storing all patch scripts in `/data/truecloud-patch/` (persistent ZFS dataset).
2. Registering a **PREINIT** `initshutdownscript` (stored in the TrueNAS database) that runs `apply.sh` on every boot before `middlewared` starts.
3. `apply.sh` re-installs `sitecustomize.py` into Python site-packages and re-patches the Angular bundle each boot.

---

## Install

Run on your TrueNAS box (as root):

```bash
git clone https://github.com/sudolulo/truenas-truecloud-patch.git
cd truenas-truecloud-patch
bash install.sh
```

Then refresh your browser. S3 and B2 credentials will now appear in the TrueCloud Backup task form.

---

## Creating a task via CLI

If you prefer the API over the UI (or want to script it):

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

Get an API key from **TrueNAS UI → System → API Keys**.

---

## Uninstall

```bash
bash /path/to/truenas-truecloud-patch/uninstall.sh
```

Restores the original UI bundle and removes the PREINIT hook. The backend patch disappears automatically on the next `middlewared` restart once `sitecustomize.py` is removed.

---

## Troubleshooting

**Apply log** (check after reboot or install):
```bash
cat /data/truecloud-patch/apply.log
```

**Verify backend patch is active** (run while middlewared is running):
```bash
midclt call cloud_backup.transfer_setting_choices  # should return without error
python3 -c "
from middlewared.rclone.remote.b2 import B2RcloneRemote
print('B2 restic:', hasattr(B2RcloneRemote, 'get_restic_config'))
"
```

**UI pattern not found warning**  
The Angular bundle's structure changed in a TrueNAS update. Open an issue with your TrueNAS version; the patch may need a regex update.
