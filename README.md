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

If TrueNAS adds native B2 or S3 support to TrueCloud Backup, the patch
detects it and degrades gracefully — see [Native support](#if-truenas-adds-native-support) below.

---

## What is actually patched

**Nothing in TrueNAS's persistent database or configuration is modified.**
The patch operates on two files that live in `/usr/` (which TrueNAS replaces
on every update) and are therefore re-applied automatically on every boot.

| Layer | What changes | Technique |
|---|---|---|
| **Backend** | `B2RcloneRemote` gains `get_restic_config()` — skipped automatically if TrueNAS already provides one on the class. `restic.py` URL builder is fixed: strips the stray leading slash and converts the slash separator to a colon (`b2:bucket:path`), which is the format restic 0.16.x expects. URL wrapper is a no-op if the URL is already correctly formed. | Direct file patch in the overlay (primary, delegates URL logic to `sitecustomize.py`) + `sitecustomize.py` import hook (belt-and-suspenders) |
| **UI** | The Angular bundle's `filterByProviders` binding is widened from `["STORJ_IX"]` to `["STORJ_IX","S3","B2"]` | In-place text replacement in the compiled JS chunk; original is backed up |

Both changes are **fail-safe**: if a patch cannot be applied (e.g. TrueNAS
restructured the relevant code), middlewared starts normally with Storj-only
support and the reason is logged to `apply.log` in your repo root.

## Supported providers after patching

| Provider | Credential type in TrueNAS |
|---|---|
| Backblaze B2 (native B2 API) | `B2` |
| AWS S3, Wasabi, Cloudflare R2, MinIO, and any S3-compatible endpoint | `S3` |
| Storj (unchanged) | `STORJ_IX` |

## How persistence works

TrueNAS SCALE updates replace `/usr/` entirely. The patch survives by keeping
this repository on a **persistent ZFS pool** (your data pool, not `/tmp` or a
system path) and registering a **PREINIT initshutdownscript** in the TrueNAS
database. On every boot, `patch/apply.sh` runs from the repo before
`middlewared` starts, placing `sitecustomize.py` in the correct site-packages
directory and re-patching the UI bundle.

If `/usr` is a read-only filesystem, `apply.sh` handles this automatically by
mounting a writable [overlayfs](https://docs.kernel.org/filesystems/overlayfs.html)
on top of the relevant directories. The overlay lives in `/run` (tmpfs) and is
recreated on every boot. No extra configuration is needed.

## Python version compatibility

`sitecustomize.py` uses the `find_spec` / `exec_module` import hook API
(Python 3.4+; the older `load_module` form was removed in Python 3.12).
Compatible with all Python versions shipped by TrueNAS SCALE.

---

## Install

Clone the repository to a **persistent ZFS pool** so it survives OS updates,
then run `install.sh` from there:

```bash
# Replace /mnt/tank with your pool name
git clone https://github.com/sudolulo/truenas-truecloud-patch.git \
    /mnt/tank/truenas-truecloud-patch
cd /mnt/tank/truenas-truecloud-patch
bash install.sh
```

The directory you clone into becomes the permanent install location. The PREINIT
boot hook points to it — **do not delete or move the repo after install.**

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
# Replace /mnt/tank/truenas-truecloud-patch with your clone path

# List your cloud credentials to find the right ID
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py \
    --host 192.168.1.1 --api-key <key> list-credentials

# Create a task with a B2 credential (id=3)
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py \
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
bash /mnt/tank/truenas-truecloud-patch/uninstall.sh
```

Replace the path with your clone location. Removes the PREINIT hook,
`sitecustomize.py`, and restores the original UI bundle from backup. The
backend changes vanish on the next `middlewared` restart.

---

## Restoring from a TrueCloud Backup

TrueCloud Backup uses [restic](https://restic.net/) under the hood. Restores
are done with the `restic` command directly — TrueNAS does not yet expose a
restore UI for TrueCloud Backup tasks.

### 1. Find the restic binary

```bash
which restic 2>/dev/null || find /usr -name restic -type f 2>/dev/null | head -1
```

Use that path in the commands below (referred to as `restic`).

### 2. Gather your repository details

You need three things from the task you created:

| Detail | Where to find it |
|---|---|
| **Bucket** and **folder** | TrueNAS UI → Data Protection → TrueCloud Backup → edit the task → Attributes |
| **Credentials** (key ID + secret) | TrueNAS UI → Credentials → Backup Credentials → edit the credential |
| **Repository password** | The `--password` value you supplied when creating the task |

### 3. Set environment variables

**Backblaze B2:**
```bash
export B2_ACCOUNT_ID="your-key-id"
export B2_ACCOUNT_KEY="your-application-key"
export RESTIC_PASSWORD="your-repo-password"
REPO="b2:your-bucket:your-folder"   # restic 0.16.x uses colon, not slash
```

**S3-compatible (AWS S3, Wasabi, Cloudflare R2, MinIO, etc.):**
```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export RESTIC_PASSWORD="your-repo-password"
# Use just the hostname as the endpoint — no https:// prefix:
REPO="s3:s3.wasabisys.com/your-bucket/your-folder"   # Wasabi example
# REPO="s3:s3.amazonaws.com/your-bucket/your-folder" # AWS S3
# REPO="s3:<account>.r2.cloudflarestorage.com/your-bucket/your-folder" # R2
```

### 4. List snapshots

```bash
restic -r "$REPO" snapshots
```

Output example:
```
ID        Time                 Host        Tags  Paths
──────────────────────────────────────────────────────────
a1b2c3d4  2026-06-01 02:00:05  truenas           /mnt/tank/data
e5f6a7b8  2026-06-08 02:00:07  truenas           /mnt/tank/data
```

### 5. Restore files

**Restore everything from the latest snapshot to a temporary location:**
```bash
restic -r "$REPO" restore latest --target /mnt/tank/restore-tmp
```

**Restore a specific snapshot by ID:**
```bash
restic -r "$REPO" restore a1b2c3d4 --target /mnt/tank/restore-tmp
```

**Restore only specific paths from within a snapshot:**
```bash
restic -r "$REPO" restore latest \
    --include /mnt/tank/data/important-dir \
    --target /mnt/tank/restore-tmp
```

**Browse a snapshot without extracting (useful for finding the right file):**
```bash
restic -r "$REPO" ls latest
```

### 6. Notes

- Restore to a **different path** first, then move files into place after
  verifying. Restoring directly over a live dataset can cause data loss if
  the snapshot is incomplete or from the wrong point in time.
- If you created the task with `--snapshot` (ZFS snapshot before each run),
  the restic snapshot captures the dataset at a consistent point in time.
- Use `restic check -r "$REPO"` periodically to verify repository integrity.

---

## If TrueNAS adds native support

When a TrueNAS update ships native B2 or S3 support in TrueCloud Backup, the
patch handles each component as follows:

| Component | What happens | Action needed |
|---|---|---|
| **B2 `get_restic_config`** added directly to `B2RcloneRemote` | `__dict__` guard detects it; our method is **not attached** | None — native version used automatically |
| **restic.py URL builder** fixed to emit `b2:bucket:path` directly | Our wrapper sees no `/` to fix; it becomes a **no-op** | None — correct URL passes through unchanged |
| **`get_restic_config` moved** out of `restic.py` entirely | `NameError` guard in the patched file catches it; wrapper silently does nothing | None — but run `verify` to confirm state |
| **B2 credential schema changed** (e.g. `provider["account"]` renamed) | Our B2 config function raises `KeyError`; backup task fails | Uninstall or update the patch |
| **B2 `get_restic_config`** added to a **base class** (not `B2RcloneRemote`) | `__dict__` check misses it; our method is attached and **shadows** the native one | Uninstall the patch |

**Recommended check after any TrueNAS update that adds TrueCloud provider
support**: run `python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify`
and attempt a B2 backup. If both pass, the patch is coexisting correctly. If
the backup fails with a credential or URL error that worked before the update,
uninstall the patch — TrueNAS has shipped a conflicting implementation.

---

## After a TrueNAS update

1. Check the log: `cat /mnt/tank/truenas-truecloud-patch/apply.log | tail -30`
2. If you see "WARNING: … pattern not found", the UI patch needs updating.
   [Open an issue](https://github.com/sudolulo/truenas-truecloud-patch/issues)
   with your TrueNAS version number.
3. The backend patch (B2 support + URL fix) is more stable — check that a
   B2 backup job still completes successfully after any update.

---

## Emergency recovery

### middlewared won't start

Run this from the TrueNAS shell (local console, SSH, or the debug shell in
the UI):

```bash
bash /mnt/tank/truenas-truecloud-patch/recover.sh
```

Replace the path with your clone location. This creates a kill-switch file
(`disabled`) in the repo root. `sitecustomize.py` checks for it at Python
startup; if present, the import hook is skipped entirely and middlewared starts
clean with Storj-only support. Nothing else on your system is affected.

If you cannot run a script and only have a bare shell prompt:

```bash
touch /mnt/tank/truenas-truecloud-patch/disabled
systemctl restart middlewared
```

If you don't remember where you cloned the repo (midclt won't work while middlewared is
down), find the path two ways:

```bash
# Option 1 — search the filesystem:
find /mnt -name "recover.sh" -path "*/truenas-truecloud-patch/*" 2>/dev/null

# Option 2 — query the TrueNAS database directly:
sqlite3 /data/freenas-v1.db \
    "SELECT script FROM initshutdownscript WHERE comment = 'TrueCloud provider patch (S3/B2)';"
```

The `script` column shows the full path to `patch/apply.sh`; your clone root is one
level up (strip `/patch/apply.sh` from the end). Then run the `touch` command above
with that path.

If middlewared **still** won't start after the kill switch is set, the problem
is unrelated to this patch. Check:

```bash
journalctl -u middlewared -n 50
```

To re-enable the patch once you have investigated:

```bash
rm /mnt/tank/truenas-truecloud-patch/disabled
bash /mnt/tank/truenas-truecloud-patch/patch/apply.sh
```

---

### Web UI is blank or broken

If the TrueNAS web interface loads blank or shows JavaScript errors, the
Angular bundle may have been interrupted mid-write (e.g. power cut during
boot). The original bundle is always backed up before patching, so recovery
is straightforward:

```bash
# Find the backup (the path varies by TrueNAS version):
find /usr/share/truenas /usr/share/truenas-ui /var/www/truenas -name "*.js.pre-truecloud-patch" 2>/dev/null

# Restore it — substitute the actual path from the find output:
mv /usr/share/truenas/webui/main.XXXXXXXX.js.pre-truecloud-patch \
   /usr/share/truenas/webui/main.XXXXXXXX.js
```

Refresh your browser. The UI will return to normal (Storj-only until the
patch re-runs at next reboot, or you run
`bash /mnt/tank/truenas-truecloud-patch/patch/apply.sh` manually).

---

### Backend verify shows FAIL

```bash
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify
```

If one or more entries show `[FAIL]`:

1. **Check the apply log** for errors during the last boot:
   ```bash
   cat /mnt/tank/truenas-truecloud-patch/apply.log | tail -40
   ```
2. **Check middlewared's own log** for Python tracebacks:
   ```bash
   grep -i "truecloud\|traceback\|error" /var/log/middlewared.log 2>/dev/null | tail -30
   journalctl -u middlewared -n 50
   ```
3. **A FAIL is non-fatal.** middlewared runs normally; the affected provider
   falls back to Storj-only. Your existing backups are not at risk.
4. **If the detail says the module doesn't exist**, a TrueNAS update renamed
   or restructured the internal API.
   [Open an issue](https://github.com/sudolulo/truenas-truecloud-patch/issues)
   with your TrueNAS version number and the full verify output.

---

## Troubleshooting

**Apply log** (check after each reboot or install):
```bash
cat /mnt/tank/truenas-truecloud-patch/apply.log
```

**Verify backend patch is loaded** (while middlewared is running):
```bash
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify
```
This reads `hook_status.json` in your repo root. The file is written by
`apply.sh` at install/boot time and reflects whether the direct file patches
to `b2.py` and `restic.py` were applied successfully. Does not require
`--host` or `--api-key`.

**Middlewared log:**
```bash
grep truecloud-patch /var/log/middlewared.log 2>/dev/null | tail -20
journalctl -u middlewared -n 100 2>/dev/null | grep truecloud-patch
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
the credential settings.

**`create_task.py` SSL error connecting to TrueNAS**
`create_task.py` talks to the **TrueNAS API**, not your S3 endpoint, and
verifies its TLS certificate. If your NAS uses a self-signed certificate,
pass `--insecure` — but be aware this disables certificate verification for
the API call that transmits your TrueNAS API key. Adding your NAS certificate
to your system's trust store is safer.
