# What has actually been run

The support matrix in the README is **static analysis**: it proves the patch's
assumptions about middlewared still hold. That is a strictly weaker claim than "a
backup ran and a restore came back". This file is the stronger claim, and it is
maintained by hand, because the only way to fill it in is to do it.

If you are deciding whether to trust this with your backups, read this file, not the
matrix.

---

## v0.7.0 — TrueNAS 25.10.4 (production hardware)

Six live TrueCloud tasks, all `snapshot = true`, backing up to Backblaze B2. Three were
exercised end to end, chosen to cover the three shapes the code handles differently:

| Task | Path | Shape | Result |
| --- | --- | --- | --- |
| 5 | `/mnt/Tap` | 191 nested datasets staged, 282-snapshot recursive tree | SUCCESS |
| 7 | `/mnt/Tank/backups` | 215 filesystems **+ 2 zvols** | SUCCESS |
| 9 | `/mnt/Tank/flan` | **no** nested filesystem children | SUCCESS |

After every run: **0 orphaned snapshots, 0 leaked bind mounts, 0 stale sidecars.**

**The restore.** `apps/vaultwarden/data/config.json` — a file inside a *child* dataset,
which is exactly what stock TrueNAS cannot capture — was restored from B2 and compared
against the live file:

    live      f809df6ba231986b1ba824044228a03a   1808 bytes
    restored  f809df6ba231986b1ba824044228a03a   1808 bytes
    => byte-identical

**The collector earned its keep on real data.** The pool was already carrying an orphan:
`Tap/apps/prometheus@cloud_backup-5-20260713202355`, left behind by an earlier run when
ZFS's automount held the snapshot busy past all four retries. The first v0.7.0 run found
it by name, reclaimed it, and the pool's snapshot count went 2148 → 2147. That is the
garbage collector doing the job it was written for, against a leak that was already
there and that nothing else would ever have found.

**Boot path.** `apply.sh` is registered as a PREINIT `initshutdownscript`; it was
re-run against the live middleware and left exactly one `TRUECLOUD_PATCH` marker in
each patched module (a second copy stacked into a live middlewared module would break
the box at boot). It correctly detected the box as **async** (`cloud_backup is async
(TrueNAS <= 25.10)`) and injected the matching wrappers.

**Upgrade path.** `update.sh` was used to move the box from the release candidate to
the stable tag, in detached HEAD at `v0.7.0`, which is how a user's box actually
upgrades.

## v0.7.0 — TrueNAS 26.0.0-BETA.1 (VM)

A throwaway VM whose pool reproduces the production pool's *shape* — 292 datasets, 26
`legacy` mountpoints, nesting five deep — because every bug found on the real box came
from the shape of the pool, not the bytes in it. MinIO was not used; `rclone serve s3`
(already on the box) provided the S3 target, so no real B2 credential ever entered the
VM.

* 274-snapshot recursive backup of the 292-dataset pool. 0 orphans, 0 leaked mounts.
* Restored `ix-apps/app_mounts/vaultwarden/pgData` — **four levels deep, and a dataset
  that middleware's own `pool.dataset.query` hides from itself** — byte-identical.
* The zvol-orphan case was **reproduced with the fix disabled** (one orphan per zvol,
  every run, backup green), then **closed with it enabled**. See the CHANGELOG entry
  for why TrueNAS 26 decides `recursive` by a different rule than this patch decides
  `nested`.

## What is NOT covered

* **24.10 and 25.04** are `ok` in the matrix — the assumptions hold, checked against
  iX's source — but nobody has run a backup on them. The matrix says so.
* **master** is BROKEN, and correctly reports so: iX renamed the leading parameters of
  `get_restic_config` and `restic_backup`. It is not a shipped release; the daily
  compatibility bot files it, and `apply.sh` would refuse to apply the modules on a box
  running it.
* A **reboot** of the production box has not been done on v0.7.0. `apply.sh` was
  re-executed by hand against the live middleware, which exercises the same code path,
  but the PREINIT ordering itself has only been proven on earlier versions.
