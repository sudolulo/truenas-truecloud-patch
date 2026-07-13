# Nested-dataset snapshots

> Part of [truenas-truecloud-patch](../README.md).

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

> **Two mechanisms clean up, and the second exists because the first can be destroyed.**
>
> 1. **The sidecar** records exactly which snapshots a run pinned, and is removed only
>    on a confirmed-clean sweep. Precise, and it survives a middlewared restart.
> 2. **The garbage collector** finds leftovers by *name*, so it still works when the
>    sidecar is gone — and it can be: **the sidecar lives in `/run`, which is tmpfs.** A
>    reboot mid-backup takes it, and with it the only record of a 250-snapshot tree.
>
> The collector runs at the start of every backup, after the sidecar reclaim. It will
> only touch a snapshot named `<dataset>@<task>-<timestamp>` that is not the current
> run's, has **nothing mounted from it** (which is what protects a concurrently-running
> backup), and is **over an hour old**. Periodic `auto-*` snapshots, other tasks'
> snapshots, and anything you made by hand are structurally out of reach.


> **A snapshot may survive a run, and that is expected.** ZFS **automounts**
> `<dataset>/.zfs/snapshot/<snap>` the moment it is read, and holds it for
> `zfs_expire_snapshot` seconds (**300** by default) after the last access. So
> whatever restic read *last* is still pinned when we try to destroy it, and
> `zfs destroy` refuses with `dataset is busy`.
>
> The patch unmounts those automounts itself and retries, which clears ~255 of 256 on
> a real pool. The one that remains is **logged, its sidecar is kept, and the next run
> reclaims it before doing anything else** — so the leak is bounded at a single cycle
> instead of growing forever. Seeing one `could not delete snapshot … it will be
> reclaimed on the next run` in the log is normal. Seeing the count *grow* run over run
> is not, and would be a bug.
>
> This is why the sidecar is removed **only on a confirmed-clean sweep**: it is the
> only record those snapshots exist, and a run that dropped it while they were still
> around would orphan them permanently. That is precisely what happened before this was
> fixed.


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
| Web UI blank after a patch | A bad pattern unbalanced the bundle. `apply.sh` now refuses to write in that case, but if you hit it on an older version: restore `chunk-*.js.pre-truecloud-patch` over the live chunk, then re-run `install.sh`. (`MARKER` makes an already-patched file skip, so the patch cannot heal a corrupted bundle by itself.) |
| Stale mounts under `/run/truecloud-nested` | A crashed run. The next backup tears them down. To clear them now: `python3 patch/truecloud_nested.py cleanup` (also run by `uninstall.sh` and `recover.sh`). It names any ZFS snapshot an interrupted run left pinned. |

