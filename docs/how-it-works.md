# How it works

> Part of [truenas-truecloud-patch](../README.md).

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
| **nested** (opt-in) | `_truecloud_nested.py` is installed into `plugins/cloud/`, and `plugins/cloud/{snapshot,crud}.py` + `plugins/cloud_backup/sync.py` are patched so `snapshot = true` works on a dataset that has child datasets. See [below](nested-snapshots.md). | New module + file patches inside the overlayfs upper layer |

All changes are **fail-safe**: if a patch cannot be applied (e.g. TrueNAS
restructured the relevant code), middlewared starts normally, the affected module
is simply inactive, and the reason is logged to `apply.log` in your repo root. The
two modules are independent — one failing or going native does not disable the
other.


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
3. **`apply.sh` runs** (`ix-preinit.service`). Before it patches anything it runs
   the **compatibility preflight** ([`tools/compat.py`](../tools/compat.py)) against
   the middlewared that is *actually installed*, and **any module whose assumptions
   no longer hold is not applied** — see [TrueNAS
   compatibility](../README.md#truenas-compatibility). What survives that check gets applied:
   it mounts the writable overlay (upper layer in `/run`), patches `b2.py` and
   `restic.py` on disk inside it, patches the UI bundle, and writes `apply.log` and
   `hook_status.json`.

   An incompatible module is skipped **for this boot only**. It is not the kill
   switch: install a release that supports your TrueNAS and the patch re-applies
   itself on the next boot, with no manual step. (The kill switch is permanent and
   is set only when TrueNAS has made the patch *unnecessary* — a different
   situation, and the opposite conclusion.)
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
[Troubleshooting](recovery.md) if it persists beyond boot.

Manual runs of `bash patch/apply.sh` never trigger the restart — that only
happens in boot context. `install.sh` and `recover.sh` perform their own
explicit restarts instead, which is why a manual re-apply must be followed by
`systemctl restart middlewared`.

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

A TrueNAS update replaces `/usr/` wholesale, wiping the patch. You do **not** need
to reinstall: `patch/apply.sh` runs at every boot and re-applies itself from your
clone. But it targets internal APIs with no stability contract, so an update *can*
break it — and the failure is quiet by design (middlewared starts fine; the patch
just doesn't).

**Check the log after any TrueNAS update:**

```bash
tail -30 /mnt/tank/truenas-truecloud-patch/apply.log
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify
```

| What you see | What it means |
|---|---|
| `[OK] providers`, `[OK]`/`[SKIP] nested_snapshots` | Fine. Nothing to do. |
| `WARNING: … pattern not found` (UI) | The Angular bundle changed. The UI dropdown reverts to Storj-only, but **backups keep working** — create tasks with `create_task.py` meanwhile, and [open an issue](https://github.com/sudolulo/truenas-truecloud-patch/issues) with your TrueNAS version. |
| `WARNING: truecloud-patch is NOT COMPATIBLE with this TrueNAS version` | This TrueNAS changed middleware underneath the patch, and the named module was **deliberately not applied** — see `incompatible.json` for exactly which assumption broke. TrueNAS is left stock, so nothing is half-patched. Check [TrueNAS compatibility](../README.md#truenas-compatibility), then `bash update.sh` once a release supports your version; it re-applies itself on the next boot. This is **not** the kill switch and needs no manual reset. |
| `[FAIL] providers` | **Your B2/S3 backups will not run.** middlewared is fine, but the credential/URL handling is gone. Open an issue with your version. |
| `[FAIL] nested_snapshots` | The stock guard is back, so tasks with `snapshot = true` on a nested dataset will fail validation. Turn the option off on those tasks until it's fixed. |

"Fail-safe" means *the box stays up* — not that your backups keep running. A
`[FAIL] providers` is a broken backup, so check the log rather than assume.

Then update the patch itself if a newer release fixes it:

```bash
bash /mnt/tank/truenas-truecloud-patch/update.sh
```

---



## TrueNAS 26

TrueNAS 26 changes three things underneath the nested module. **Each one alone is
backup-breaking**, and none of them is visible from the `cloud_backup` files:

| what changed | what it would do |
| --- | --- |
| `cloud_backup` rewritten **async → synchronous** | an `async def` wrapper hands `sync.py` a coroutine where it unpacks a tuple |
| `get_dataset_recursive()` **deleted** from `plugins/cloud/snapshot.py` | `NameError` — the injected block called it out of the host module's namespace |
| `plugins/zfs_/dataset.py` and `zfs_/snapshot.py` **deleted** | `zfs.dataset.query`, `zfs.snapshot.query` and `zfs.snapshot.delete` all vanish. 26 uses `filesystem.statfs` and `zfs.resource.*` |

All three are fixed as of **v0.7.0**, and 26 is supported.

The first two were straightforward: the patch reads which flavour of `cloud_backup`
your box declares and injects the wrapper that matches (one implementation of the real
logic, two thin wrappers), and it carries its own copy of the deleted helper.

The third was not, and it is the one that would have hurt most — `zfs.snapshot.delete`
is what sweeps the recursive snapshot, and without it **every run would orphan one
snapshot per descendant dataset (250 on a real pool), forever, while reporting
success.**

The obvious port is to the public `pool.dataset.query` / `pool.snapshot.query`. Those
methods exist, are documented, and are covered by iX's deprecation policy — and they
are **not like-for-like replacements**. They apply a *visibility policy*: they hide the
datasets TrueNAS considers its own (`ix-apps/*`, `.system/*`, `.ix-virt/*`). On a real
pool that is **84 of 270 datasets, including live application data.** Staging from that
view would have omitted every one of them from the backup — and the planner would never
have seen them, so they would not have appeared in its "skipped" list either. A green
backup, quietly missing data. The snapshot query lies the same way, so the sweep would
have orphaned one snapshot per hidden dataset.

No source analysis could have caught that. The methods are all present and correctly
shaped. Only running it could, which is why it took a real 26 box.

So the module now follows one rule:

> **Read the truth from ZFS. Make changes through middleware.**

Enumeration is `zfs list` — no policy can filter it, and it behaves identically on every
release, which also means one code path instead of a version conditional. Mutation stays
a middleware call, so TrueNAS's own bookkeeping stays consistent; an exact-name delete
works fine even on a dataset the query hides. It is only enumeration that lies.

The snapshot *delete* still needs a namespace, and no single one spans every release —
24.10 and 25.04 have `zfs.snapshot`, 26 has only `pool.snapshot`, 25.10 has both. So it
is resolved at runtime, by asking whether the namespace can actually delete. `tools/compat.py`
asks the identical question against iX's source, and a test binds the two lists together,
so what CI verifies and what runs cannot drift apart.

### The one 26 changed that nothing warned about

Stock decides whether to take a **recursive** snapshot by its own rule, and on 26 that
rule stopped being ours:

| | decides `recursive` by |
| --- | --- |
| stock ≤ 25.10 | `get_dataset_recursive()` — the same function this patch vendors |
| **stock 26** | `filesystem.statfs`: `recursive = (path == the dataset's mountpoint)` |
| this patch | `get_dataset_recursive()` — is a mounted *filesystem* child under the path? |

Up to 25.10 those were the *same question*, so a snapshot the patch declined to stage
provably had no children and stock's non-recursive delete was correct. On 26 they
disagree: a dataset whose only descendants are **zvols** or **legacy-mountpoint**
datasets gets a recursive snapshot, while the patch sees nothing to stage. Stock then
destroys the parent only — and with no staging tree there was no sidecar, and the
garbage collector only ever ran from the staging path. Nothing on the box would ever
have found the children.

It was reproduced on a 26 VM (one orphan per zvol, every run, backup green) and closed:
**ownership of the sweep is no longer conditional on staging.**

`master` (development after 26) **does** report BROKEN: iXsystems are still reshaping
these functions there, renaming `middleware` → `context` and `cloud_backup` → `entry`
and adding a required `credentials` parameter. That is a moving target and is
deliberately not chased; the check keeps reporting it until it settles into a beta,
which is when it becomes worth fixing. Until then, a box running master would simply
not get the modules — `apply.sh` refuses to apply a module whose assumptions no longer
hold, and says why in `apply.log`.
