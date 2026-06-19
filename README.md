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
detects it at boot, disables itself, and tells you to run `uninstall.sh` —
see [Native support](#if-truenas-adds-native-support) below.

---

## What is actually patched

**Nothing in TrueNAS's persistent database or configuration is modified.**
On every boot, `patch/apply.sh` runs as a PREINIT script before middlewared
starts. It mounts a writable
[overlayfs](https://docs.kernel.org/filesystems/overlayfs.html) over the
relevant directories in `/usr/` (upper layer in `/run` tmpfs), then patches
`b2.py` and `restic.py` inside that overlay. The overlay is volatile — it
exists only for the current boot — but the PREINIT script recreates it
automatically on every subsequent boot. Nothing in `/usr/` is written to
directly.

| Layer | What changes | Technique |
|---|---|---|
| **Backend** | `B2RcloneRemote` gains `get_restic_config()` — skipped automatically if TrueNAS already provides one on the class. `restic.py` URL builder is fixed: strips the stray leading slash and converts the slash separator to a colon (`b2:bucket:path`), which is the format restic 0.16.x expects. URL wrapper is a no-op if the URL is already correctly formed. | File patch applied inside the overlayfs upper layer |
| **UI** | The Angular bundle's `filterByProviders` binding is widened from `["STORJ_IX"]` to `["STORJ_IX","S3","B2"]` | In-place text replacement in the compiled JS chunk; original is backed up before patching |

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
database. On every boot, `patch/apply.sh` runs before `middlewared` starts. It
mounts a writable [overlayfs](https://docs.kernel.org/filesystems/overlayfs.html)
over the relevant directories (upper layer in `/run`, recreated each boot), then
patches `b2.py` and `restic.py` directly in that overlay and re-patches the UI
bundle. No extra configuration is needed.

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

The directory you clone into becomes the **permanent install location**. The
PREINIT boot hook is registered with the exact path you chose, and TrueNAS will
call that path on every boot.

> **Do not delete or move the repository after install.**
> If you need to relocate it, run `bash uninstall.sh` first, move the directory,
> then run `bash install.sh` again from the new location. Deleting the repo
> without uninstalling leaves a dangling PREINIT hook in the TrueNAS database —
> if that happens, see [Emergency recovery](#emergency-recovery) below.

Refresh your browser. S3 and B2 credentials now appear in the
**Data Protection → TrueCloud Backup → Add** credential dropdown.

## Updating

To update to a new version of the patch:

```bash
cd /mnt/tank/truenas-truecloud-patch

# If install.sh was previously run as root, the .git directory may be owned
# by root. Fix it first, or just pull as root:
sudo git pull          # easiest option
# — or —
sudo chown -R $(whoami) .git && git pull

bash install.sh
```

`install.sh` clears any stale kill switch, re-applies the updated patches,
and restarts middlewared. Run `python3 patch/create_task.py verify` afterwards
to confirm the patches loaded successfully.

Check [CHANGELOG.md](CHANGELOG.md) to see what changed between versions.

---

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
unmounts the overlay (restoring the original backend files immediately),
and restores the original UI bundle from backup.

---

## If TrueNAS adds native support

`apply.sh` checks at every boot whether TrueNAS has shipped native B2 restic
support (by inspecting `B2RcloneRemote.__dict__`). If it has:

1. The kill switch (`disabled` file) is set — no patching on any future boot.
2. Any active overlays are unmounted immediately.
3. The following message is written to `apply.log`:

```
NOTICE: TrueNAS now provides native B2 restic support — truecloud-patch is no longer needed.
NOTICE: Setting kill switch; patching will be skipped on all future boots.
NOTICE: Run the following to fully remove the patch:
NOTICE:   bash /mnt/tank/truenas-truecloud-patch/uninstall.sh
```

Check the log after any TrueNAS update:
```bash
cat /mnt/tank/truenas-truecloud-patch/apply.log | tail -20
```

**Scenarios where the auto-detect may not fire** (manual check needed):

| Scenario | What happens | Action |
|---|---|---|
| B2 support added to a **base class** (not `B2RcloneRemote` directly) | `__dict__` check misses it; our method shadows native | Uninstall manually |
| B2 **credential schema changed** (e.g. `provider["account"]` renamed) | `KeyError` on first backup | Uninstall or update the patch |
| **URL builder** fixed but B2 class unchanged | URL wrapper becomes a no-op; no harm, but patch is dead weight | Uninstall at your convenience |

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
(`disabled`) in the repo root, unmounts the overlay so the original files are
visible immediately, then restarts middlewared. No reboot required.

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
Reads `hook_status.json` written by `apply.sh` at boot. Reflects whether the
overlay patches to `b2.py` and `restic.py` were applied successfully. Does not
require `--host` or `--api-key`.

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

**`create_task.py` SSL error connecting to TrueNAS**
`create_task.py` talks to the **TrueNAS API**, not your S3 endpoint, and
verifies its TLS certificate. If your NAS uses a self-signed certificate,
pass `--insecure` — but be aware this disables certificate verification for
the API call that transmits your TrueNAS API key. Adding your NAS certificate
to your system's trust store is safer.
