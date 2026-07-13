# truenas-truecloud-patch

Extends TrueNAS SCALE's **TrueCloud Backup** feature to:

- work with S3-compatible providers and native Backblaze B2, instead of Storj only;
- take **consistent snapshots of datasets that have child datasets** — which is
  every box running Apps (see [Nested-dataset snapshots](#nested-dataset-snapshots)).

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

## Before you install

This project is unofficial and not affiliated with iXsystems. A few things worth
knowing:

- It targets **internal middleware APIs** with no stability contract, so a
  TrueNAS update can break it. Every patch is fail-safe: if it can't apply,
  middlewared starts normally and the reason is logged to `apply.log`. Check the
  log after an update.
- If you file a TrueNAS bug report, **remove the patch first** and reproduce on a
  stock system.
- **Test your restores.** True of any backup, but it matters more here — see
  [Verifying it works](#verifying-it-works).
- Provided as-is, no warranty. See LICENSE.

The patch is two independent modules — **providers** (B2/S3) and **nested**
(snapshots on nested datasets) — and each retires on its own once TrueNAS ships
that capability natively. See [Native support](#if-truenas-adds-native-support).

## Development

Parts of this project were written with AI assistance (Claude). All of it is
reviewed and tested before release; the test suite and CI exist in large part to
make that review meaningful. Bugs are mine.

```bash
pip install pytest ruff
ruff check patch tests
pytest tests
```

CI runs shellcheck, `bash -n`, ruff, and pytest on Python 3.11–3.13. The tests
include a pass that `compile()`s the `*_BLOCK` strings in `patch/apply.sh` —
those are Python source appended into live `middlewared` modules, so a syntax
error there would break the box at boot.

---

## What is actually patched

**Nothing in TrueNAS's persistent database or configuration is modified**
(other than the boot-hook entry itself). On every boot, `patch/apply.sh` runs
as a PREINIT script. It mounts a writable
[overlayfs](https://docs.kernel.org/filesystems/overlayfs.html) over the
relevant directories in `/usr/` (upper layer in `/run` tmpfs), then patches
`b2.py` and `restic.py` inside that overlay. The overlay is volatile — it
exists only for the current boot — but the PREINIT script recreates it
automatically on every subsequent boot. Nothing in `/usr/` is written to
directly.

PREINIT scripts are executed *by* middlewared, which by then has already
imported the stock modules — so after patching, `apply.sh` schedules a single
detached middlewared restart (transient systemd unit `truecloud-mw-restart`
running `patch/wait_restart.sh`) that loads the patched modules once boot has
*actually* settled: the script waits for the systemd boot job queue to drain
and for the docker/apps state machine to reach a terminal state before
restarting. Expect one middlewared restart shortly after every boot; the UI
and API are briefly unavailable while it happens, and running services are
not affected.

| Module | What changes | Technique |
|---|---|---|
| **providers** | `B2RcloneRemote` gains `get_restic_config()` — skipped automatically if TrueNAS already provides one on the class. `restic.py` URL builder is fixed: strips the stray leading slash and converts the slash separator to a colon (`b2:bucket:path`), which is the format restic 0.16.x expects. URL wrapper is a no-op if the URL is already correctly formed. | File patch applied inside the overlayfs upper layer |
| **providers** (UI) | The Angular bundle's `filterByProviders` binding is widened from `["STORJ_IX"]` to `["STORJ_IX","S3","B2"]` | In-place text replacement in the compiled JS chunk; original is backed up before patching |
| **nested** (opt-in) | `_truecloud_nested.py` is installed into `plugins/cloud/`, and `plugins/cloud/{snapshot,crud}.py` + `plugins/cloud_backup/sync.py` are patched so `snapshot = true` works on a dataset that has child datasets. See [below](#nested-dataset-snapshots). | New module + file patches inside the overlayfs upper layer |

All changes are **fail-safe**: if a patch cannot be applied (e.g. TrueNAS
restructured the relevant code), middlewared starts normally, the affected module
is simply inactive, and the reason is logged to `apply.log` in your repo root. The
two modules are independent — one failing or going native does not disable the
other.

## Nested-dataset snapshots

**Opt-in, off by default.** It changes how backups read their source data, so it
is never enabled implicitly:

```bash
bash install.sh --enable-nested-snapshots
bash install.sh --disable-nested-snapshots
```

With neither flag `install.sh` leaves the setting alone, so `git pull && bash
install.sh` won't flip it. The providers module is unaffected either way.

Validated end to end on a live 252-dataset pool: an unattended scheduled backup
of `/mnt/Tap` built a 173-mount staging tree, completed in **18m14s**, and left
**zero** orphaned snapshots and **zero** stale mounts behind. The same backup
previously stalled at 74% for over 12 hours reading live files.

Still: verify your own first run actually contains child-dataset data before you
rely on it — see [Verifying it works](#verifying-it-works). That advice is not
boilerplate; it is the specific thing this feature exists to make true.

TrueCloud Backup's **Take Snapshot** option makes restic read from a frozen ZFS
snapshot instead of live files. Without it the backup reads data *while apps are
writing to it* — databases get captured mid-write, and an app that rewrites its
files continuously can stall a backup indefinitely as restic chases a moving
target.

Stock TrueNAS refuses to enable it on most real-world paths:

```
[EINVAL] cloud_backup_update.snapshot:
  This option is only available for datasets that have no further nesting
```

That rules out **any pool running Apps** — every app is its own dataset, usually
with `config`/`pgdata` children of its own. On a typical box that is 100+ nested
datasets, so the feature is effectively unusable exactly where it matters most.

### Why stock refuses

The guard is **correct**. `plugins/cloud/snapshot.py` already takes a *recursive*
ZFS snapshot — but it then points restic at the **parent** dataset's
`.zfs/snapshot/<snap>/` directory, and ZFS does not expose child datasets
through a parent's snapshot directory:

```
/mnt/Tap/.zfs/snapshot/<snap>/apps/                 ->  0 entries  (children invisible)
/mnt/Tap/apps/lidarr/config/.zfs/snapshot/<snap>/   ->  the real data
```

So if you just remove the validation, restic walks a near-empty tree, reports
SUCCESS, and uploads almost nothing — a green backup job protecting no data. iX
gate the config rather than ship a backup that lies about succeeding.

That is worth spelling out, because deleting those four lines in
`plugins/cloud/crud.py` is the obvious "fix" and it is the wrong one. The guard
is load-bearing: it has to be *replaced* with a working traversal, not removed.

### What this patch does instead

After the (already recursive) snapshot is taken, every descendant dataset's own
`.zfs/snapshot/<snap>` is bind-mounted into a **staging tree** that mirrors the
original layout, and restic is pointed at the staging root — a complete,
consistent, point-in-time view of the whole subtree. Only then is the guard
relaxed.

Safety properties, in order of importance:

- **Staging failure is loud.** If any descendant cannot be staged, the backup
  *fails*. A silently-incomplete backup is precisely what the stock guard exists
  to prevent, and it would be worse than not having the feature at all.
- **Post-mount verification** asserts every planned target really is a mountpoint
  and the staging root is non-empty — so this cannot regress into the empty
  backup it exists to fix.
- **The guard is relaxed last.** `apply.sh` installs the traversal, patches
  `snapshot.py`, then `sync.py`, and only then `crud.py`. Any partial failure
  leaves the guard intact and the option merely unavailable — never
  "guard removed, traversal missing".
- Datasets that cannot contribute to a file tree (`mountpoint=none|legacy`,
  unmounted, locked/encrypted) are skipped and **reported to the log** — never
  dropped silently.
- Scoped to **cloud_backup only**. Cloud Sync (rclone) shares the same
  validation mixin but has no staging teardown wired in, so its guard is
  deliberately left in place.

Side benefit: the staging root is a **stable path per task**, so restic can find
its parent snapshot between runs. Stock's `.zfs/snapshot/<name>-<timestamp>/`
path changes every run, defeating restic's parent detection and forcing a full
re-scan each time.

### Snapshot lifecycle

`zfs.snapshot.delete` defaults to **`recursive=False`**, and stock
`restic_backup()` calls it with no options. Stock is safe only because its
validation means a *recursive* snapshot never actually happens in the field.
Enabling nested datasets makes them real: on a 250-dataset pool,
`zfs snapshot -r` creates **250 snapshots**, and stock's delete removes only the
parent — orphaning **249 on every successful run** (measured, not theorised).

So the patch owns the whole lifecycle:

- **Sweeps the parent and every child**, and is idempotent against stock's
  `finally` winning the race once the mounts are released.
- **Records the snapshot in a sidecar file before mounting anything**, so a
  middlewared restart mid-backup cannot orphan the tree (this patch *schedules*
  a restart at boot, so that is not hypothetical).
- **Reclaims the tree left by a crashed run** instead of overwriting the record.
- **Deletes the tree when staging fails** — sync.py's own `finally` deletes
  *nothing* in that case, because its `snapshot` local never gets assigned.
- **Enumerates datasets *after* the snapshot, never before.** A list read
  beforehand can miss a dataset created in the gap, which the recursive snapshot
  *would* capture but the staging plan would not — a silent omission.

**Expected log noise:** stock's delete fails with `EBUSY` while the staging
mounts pin the snapshot. You will see one benign `Error deleting snapshot ...`
warning per run; the patch then unmounts and deletes the tree for real.

### Verifying it works

This feature exists because a backup can report SUCCESS while containing
nothing, so check the contents rather than the exit status:

```bash
# 1. Does the restic snapshot actually contain child-dataset data?
#    Pick a path that lives in a CHILD dataset (e.g. an app's config).
midclt call cloud_backup.list_snapshots <task_id> | head

# 2. List a child-dataset path inside the newest restic snapshot.
#    If this is empty, the staging tree did not work and you are backing up NOTHING.
midclt call cloud_backup.list_snapshot_directory <task_id> "<snapshot_id>" "/apps/lidarr/config"
```

You should see the app's real files (`lidarr.db`, `config.xml`, …). An empty
listing means the child datasets were not staged; disable the feature and open an
issue.

```bash
# 3. No snapshots may be left behind after a run.
zfs list -t snapshot -r <pool> | grep -c cloud_backup-   # expect 0 between runs

# 4. No staging mounts may be left behind.
mount | grep truecloud-nested                            # expect no output
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `This option is only available for datasets that have no further nesting` | Feature not enabled. Run `install.sh --enable-nested-snapshots`, then restart middlewared. |
| Backup fails: `dataset '…' has no snapshot '…'; refusing to back up an incomplete tree` | Working as designed — a descendant dataset was not covered by the snapshot. The backup is refused rather than silently omitting that data. |
| Backup fails: `snapshot '…' cannot be read (Permission denied)` | The snapshot exists but is unreadable. Middleware runs as root, so this indicates a real permissions problem, not a missing snapshot. |
| `cloud_backup-*` snapshots accumulating | The sweep is not running. Check `apply.log` for the nested patch applying, and confirm `sync.py` carries the `TRUECLOUD_PATCH` block. |
| Stale mounts under `/run/truecloud-nested` | A crashed run. The next backup tears them down. To clear them now: `python3 patch/truecloud_nested.py cleanup` (also run by `uninstall.sh` and `recover.sh`). It names any ZFS snapshot an interrupted run left pinned. |

## Supported providers after patching

| Provider | Credential type in TrueNAS |
|---|---|
| Backblaze B2 (native B2 API) | `B2` |
| AWS S3, Wasabi, Cloudflare R2, MinIO, and any S3-compatible endpoint | `S3` |
| Storj (unchanged) | `STORJ_IX` |

## How persistence works

Two different things must survive two different events:

| Event | What would be lost | What makes it survive |
|---|---|---|
| **Reboot** | The overlay holding the patched files lives in `/run` (tmpfs) and vanishes | The PREINIT hook re-runs `apply.sh` on every boot and schedules one middlewared restart to load the result |
| **TrueNAS update** | `/usr/` is replaced entirely; custom files in `/etc/` are wiped with the new boot environment | This repo lives on your **data pool**, and the hook registration lives in the **TrueNAS config database** — both survive updates. The first boot after an update is just a normal boot |

### What happens on every boot

1. **middlewared starts** with the stock (unpatched) modules. This is
   unavoidable: PREINIT scripts are executed *by* middlewared
   (`ix-preinit.service` → `midclt call initshutdownscript.execute_init_tasks`),
   so nothing registered there can run before it.
2. **Pools import** (`ix-zfs.service`), making `/mnt/<pool>` — and this
   repository — available.
3. **`apply.sh` runs** (`ix-preinit.service`): mounts the writable overlay
   (upper layer in `/run`), patches `b2.py` and `restic.py` on disk inside it,
   patches the UI bundle, and writes `apply.log` and `hook_status.json`.
4. **A deferred restart is scheduled.** The middlewared that is running
   imported the stock modules in step 1 and never re-imports, so the on-disk
   patch alone is not enough. `apply.sh` detects it was invoked by middlewared
   and creates a transient systemd unit (`truecloud-mw-restart`, via
   `systemd-run --no-block`) running `patch/wait_restart.sh` — detached so it
   cannot disrupt the remainder of the boot sequence.
5. **Once boot has settled, middlewared restarts once** and imports the
   patched modules from the overlay. `wait_restart.sh` holds the restart until
   the systemd boot job queue has drained (so in-flight `ix-*` units like
   `ix-reporting` finish first) *and* middlewared's docker/apps startup has
   reached a terminal state — plain unit ordering cannot see either, and
   restarting middlewared while they run kills apps and dashboard reporting
   for the whole boot. S3/B2 backup support is then active until the next
   reboot, when the cycle repeats.

What you will observe: one middlewared restart shortly after every boot (a
brief web UI/API blip; running services are unaffected). Between steps 3
and 5 there is a short window — typically well under a minute — where the UI
already shows S3/B2 (the JS bundle is read from disk per request) but the
backend is still stock. A backup job that fires inside that window fails once
with `NotImplementedError` and succeeds on its next run; see
[Troubleshooting](#troubleshooting) if it persists beyond boot.

Manual runs of `bash patch/apply.sh` never trigger the restart — that only
happens in boot context. `install.sh` and `recover.sh` perform their own
explicit restarts instead, which is why a manual re-apply must be followed by
`systemctl restart middlewared`.

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
changed in a new TrueNAS version), create tasks directly. Run this **on the
TrueNAS host** — it talks to the local middleware via `midclt`, so it needs no
host address or API key:

```bash
# Replace /mnt/tank/truenas-truecloud-patch with your clone path

# List your cloud credentials to find the right ID
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py list-credentials

# Create a task with a B2 credential (id=3)
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py create \
    --name "tank-to-b2" \
    --path /mnt/tank/data \
    --credential 3 \
    --bucket my-bucket \
    --folder backups/tank \
    --password "restic-repo-password" \
    --cache-path /mnt/tank/.restic-cache \
    --keep-last 14
```

> **Always pass `--cache-path`.** Without it TrueNAS runs restic with `--no-cache`,
> which re-fetches all repo metadata from the provider every run — glacially slow
> on large repos. Point it at a writable dir on a pool with free space.

> Versions ≤ 0.1.0 used the `/api/v2.0` REST API with `--host`/`--api-key`; those
> flags are now accepted-but-ignored (REST is removed in TrueNAS 26.04).

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

The patch is **two independent modules**, and each retires on its own — TrueNAS
is likely to ship one of these natively long before the other, and a module
going native must not take the other one down with it.

| Module | What it does | Detected as native when |
|---|---|---|
| **providers** | B2/S3 credentials for TrueCloud Backup (`b2.py`, `restic.py`, UI dropdown) | `B2RcloneRemote` carries a real `get_restic_config()` |
| **nested** | Snapshots on datasets with child datasets (`plugins/cloud/*`) | the *"no further nesting"* validation is gone from `plugins/cloud/crud.py` |

At every boot `apply.sh` checks both:

- **One module goes native** → that module is skipped and logged; the other keeps
  working, and the patch stays installed.
- **Both are done** (native, or nested was never enabled) → the kill switch
  (`disabled` file) is set, overlays are unmounted, and `apply.log` tells you to
  run `uninstall.sh`.

So on a box using only the provider patch, native B2 support retires the whole
thing as before. On a box that also uses nested snapshots, native B2 support
retires *just* that half.

Check the log after any TrueNAS update:
```bash
tail -20 /mnt/tank/truenas-truecloud-patch/apply.log
```

`hook_status.json` reports each module separately (`module.providers`,
`module.nested_snapshots`) with an `active` flag and a reason.

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
systemctl restart middlewared   # manual apply.sh runs never restart for you
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

`verify` reports one line per module:

| Label | Meaning |
|---|---|
| `[OK  ]` | Module is active and applied. |
| `[SKIP]` | Module is inactive — either TrueNAS now does it natively, or it is opt-in and switched off. **Not a failure.** `nested_snapshots` shows SKIP on a default install. |
| `[FAIL]` | Module is needed but did not apply. |

If a module shows `[FAIL]`:

1. **Check the apply log** for errors during the last boot:
   ```bash
   tail -40 /mnt/tank/truenas-truecloud-patch/apply.log
   ```
2. **Check middlewared's own log** for Python tracebacks:
   ```bash
   grep -i "truecloud\|traceback\|error" /var/log/middlewared.log 2>/dev/null | tail -30
   journalctl -u middlewared -n 50
   ```
3. **A FAIL is non-fatal.** middlewared runs normally and the other module is
   unaffected; the failed one is simply inactive. Existing backups are not at
   risk.
4. **If the detail says the module doesn't exist**, a TrueNAS update renamed
   or restructured the internal API.
   [Open an issue](https://github.com/sudolulo/truenas-truecloud-patch/issues)
   with your TrueNAS version number and the full verify output.

---

## Troubleshooting

**Backups fail with `NotImplementedError` after a reboot**

The traceback ends in `rclone/base.py` → `raise NotImplementedError` and
contains no `_tc_` frames: the running middlewared is executing stock code.
Either the deferred restart never fired, or the patch never landed on disk
this boot. Diagnose in this order:

```bash
# Did apply.sh run this boot, at which version, and did it schedule the restart?
tail -40 /mnt/tank/truenas-truecloud-patch/apply.log

# Full check — compares the running process against the patch timestamp
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify

# Did the deferred restart unit run, fail, or never get created?
systemctl status truecloud-mw-restart.service
journalctl -u truecloud-mw-restart.service --no-pager | tail -20
```

- `verify` reports the process started **before** the patch → the restart
  didn't happen. `systemctl restart middlewared` fixes it immediately; the
  journal output above tells you why it was missed.
- `apply.log` shows the kill switch is active → `rm .../disabled`, then
  `bash install.sh`.
- `apply.log` has no entry for this boot → the hook didn't run; re-run
  `bash install.sh` to re-register it.
- `apply.log` header shows `[v0.0.3]` or older → update:
  `git pull && bash install.sh` (v0.0.4 fixed patches not loading after
  reboot).

**Apply log** (check after each reboot or install):
```bash
cat /mnt/tank/truenas-truecloud-patch/apply.log
```

**Verify backend patch is loaded** (while middlewared is running):
```bash
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify
```
Reads `hook_status.json` written by `apply.sh` at boot **and** checks that the
running middlewared process started *after* the patches were applied — an
on-disk patch that middlewared has not loaded yet is reported as FAIL with
instructions. Does not require `--host` or `--api-key`.

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

**`create_task.py` — "midclt not found" or permission errors**
`create_task.py` now talks to the local middleware via `midclt`, so run it **on
the TrueNAS host** (not remotely) as a user with middleware access (root). There
is no HTTPS/API-key call anymore, so there is no TLS certificate to configure.
