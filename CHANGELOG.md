# Changelog

Work lands under **Unreleased** and stays there until a release promotes it. That
is deliberate: see [Releasing](docs/releasing.md). Twelve releases were cut on
2026-07-13, several of them fixing the release before — and with the update alert
live, every one of those interrupts every user. An alert people learn to ignore is
worse than no alert, because one day it carries a security fix.

## Unreleased
### Changed

- **`master` is now labelled `27-dev`, because it is not the next release.** iX
  branches each major onto its own `release/` line and master rolls straight on to the
  one after — on 2026-07-14 every recent commit on master targeted `27.0.0-BETA.1`
  while 26 was still in beta. So a **BROKEN** master row, rendered as
  "master _(unreleased)_", read as *"the version you are about to install is broken"*
  when the breakage was a major release away on a line nobody can download. In a table
  whose entire job is helping somebody decide whether to trust this with their backups,
  that is a false alarm in the worst possible place. The label is derived from the
  newest major in the matrix plus one, so it rolls over to `28-dev` by itself once 27
  branches.

  For the record, the breakage is `NAS-141498` (2026-06-24), "Convert cloud_backup
  plugin to the typesafe pattern": it re-signatures `restic_backup` and
  `get_restic_config`, splitting `entry`/`credentials` out of the `cloud_backup` dict.
  It is deliberately not being chased while the 27 line is still churning.

### Fixed

- **The next maintenance release was never checked, and it is the one that reaches
  users.** Shipped versions were discovered from `TS-*` tags and unreleased ones from
  `release/*` branches carrying `-BETA`/`-RC`. A branched-but-untagged *maintenance*
  release is neither: `release/25.10.5` has no tag, and its line has already shipped,
  so the "a prerelease of a shipped line is history" filter discarded it. It was
  invisible — and it is precisely what a 25.10.4 box gets on its next update. A break
  there would have reached real users before the daily check ever looked at it, on the
  only line anybody is actually running.

  A plain `release/X.Y.Z` branch is now checked when its line **has** shipped and it
  sorts **newer** than that line's newest tag. Both things that must stay out fall out
  of the same rule: `release/24.10-RC.2` sorts older than `TS-24.10.2.4` (history, not
  a warning), and iX's typo branch `release/25.20.2.2` is on a line that has no tag at
  all, so it is not a release line. This immediately surfaced two refs that had never
  been checked — `release/25.10.5` and `release/24.10.2.5` — both of which pass.

  `is_unreleased()` now keys off where a ref came from (branch = not yet shipped)
  rather than looking for `-BETA`/`-RC` in its name. Otherwise `release/25.10.5` would
  count as shipped and a break in it would fail the build as a live outage — on a
  version nobody is running yet.

- **An unchanged fingerprint froze the bug report's body, not just its comments.** Two
  questions were sharing one answer. *Have the findings changed?* gates **comments** —
  they notify, and a daily "still broken, same as yesterday" is what teaches everyone
  to ignore the one that finally matters. *Is the body still true?* gates the **body** —
  and editing an issue body notifies nobody on either forge, so keeping it honest is
  free. Conflated, the report could never be corrected while the findings held steady,
  and the fingerprint deliberately ignores everything that moves on its own — healthy
  rows, the hardware-verified column, point releases, and how a row is labelled. The
  `master` → `27-dev` relabel above would have reached the README and never the issue
  anybody actually opens. The body is now rewritten whenever it is out of date (after
  normalising line endings, so a forge round-tripping `\r\n` does not cause a rewrite
  every run) and comments remain strictly a changelog of real changes.

- **The compatibility bot filed a new duplicate bug report on every Gitea run.**
  `find_issue()` skipped pull requests by testing for the *presence* of the
  `pull_request` key. GitHub omits that key on a plain issue; Gitea sends it as
  `null`. So on Gitea every issue was discarded as a PR, the lookup always came back
  empty, and the bot took the "nothing filed yet" branch and opened a fresh report
  each run — **nine copies on the canonical forge**, four of them filed *after* the
  commit that was meant to stop precisely this. The mirror was fine, which is why it
  went unnoticed: GitHub's payload shape is the one the filter was written against.

  It is the same failure the anti-spam fix was written to prevent, moved from
  comments to issues, and it survived because `find_issue` was the only function in
  `compat_publish.py` with no test. It now has one, per forge, and the daily cron —
  which had not yet run once — no longer accumulates a report a day.

  The issue list is also requested with **both** paging parameters (`per_page` for
  GitHub, `limit` for Gitea). Each forge ignores the other's, and Gitea's default page
  is 30, so the lookup would have started missing the report again once the pile it
  was creating grew past one page.

## v0.7.0 — 2026-07-14
### Added

- **TrueNAS 26 support, verified on a real TrueNAS 26 install.** 26 deletes
  `plugins/zfs_/` outright, taking the private `zfs.dataset.query`,
  `zfs.snapshot.query` and `zfs.snapshot.delete` with it. Every one of those was on
  the nested module's critical path, so nested snapshots were **BROKEN** on 26 and
  `apply.sh` correctly refused to apply the module there.

  Snapshot **deletion** now resolves its namespace at runtime — `pool.snapshot` on
  25.10 and 26, `zfs.snapshot` on 24.10 and 25.04, because no single namespace spans
  every supported release. `tools/compat.py` checks the same list the runtime uses,
  so what CI verifies and what runs cannot drift apart.

  Hardware-verified on TrueNAS 26.0.0-BETA.1: a 274-snapshot recursive backup of a
  292-dataset pool, then a **byte-identical restore of a four-level-deep child
  dataset**.

### Fixed

- **Enumeration no longer trusts middleware's dataset and snapshot queries — they
  are filtered.** This is the important one, and it is the bug that a test VM caught
  and no amount of source analysis ever could have.

  The obvious port of the deleted private `zfs.dataset.query` was the public
  `pool.dataset.query`. It exists, it is documented, it is covered by iX's
  deprecation policy — and it is **not a like-for-like replacement**. It applies a
  *visibility policy*: it hides the datasets TrueNAS considers its own — `ix-apps/*`,
  `.system/*`, `.ix-virt/*`. On a real pool that is **84 of 270 datasets**, and
  `ix-apps` holds **live application data**.

  Staging from that view would have silently omitted every one of them. Worse,
  `plan_staging()` would never have seen them, so they would not have appeared in its
  `skipped` list either — no warning, no failure, just a green backup quietly missing
  data. That is precisely the failure this module exists to prevent. The snapshot
  query lies the same way (205 of 274), so the sweep would have orphaned one snapshot
  per hidden dataset, on every run, forever.

  The module now **reads the truth from ZFS and makes changes through middleware**:
  enumeration is `zfs list`, which no policy can filter and which behaves identically
  on every release; mutation stays a middleware call, so TrueNAS's own bookkeeping
  stays consistent. A failing `zfs list` raises rather than returning an empty list —
  "no datasets" and "the command broke" must never look the same.

  **No shipped release is affected.** v0.6.1 and earlier call the *private*
  `zfs.dataset.query`, which returns all 270 datasets. The bug existed only in the
  unreleased TrueNAS 26 port.

- **The patch now owns the snapshot sweep even when it does not stage anything.**
  Stock decides whether to take a *recursive* snapshot by its own rule, and on
  TrueNAS 26 that rule stopped being ours.

  Up to 25.10, stock's `create_snapshot` called `get_dataset_recursive()` — the same
  function this module vendors — so "stock went recursive" and "we have something to
  stage" were the *same question*, and stock's non-recursive delete was correct for
  everything the patch declined to stage. TrueNAS 26 uses `filesystem.statfs`:
  `recursive = (path == the dataset's mountpoint)`. The two rules now disagree for a
  dataset whose only descendants are **ZVOLs** or **legacy/none-mountpoint** datasets
  — stock snapshots it recursively, while the patch sees nothing to stage.

  The patch then handed the snapshot back to stock, which destroys the parent only.
  With no staging tree there was no sidecar, and the garbage collector only ever ran
  from the staging path — so nothing on the box would ever have found the children.
  Reproduced on the test VM: one orphaned snapshot per zvol, on every run, forever,
  with the backup reporting success. Ownership of the sweep is no longer conditional
  on staging.

- **The runtime resolved a *namespace*; the checker verified a *method*.** Those are
  different questions, and the gap is a false "ok". `get_service()` only proves a
  namespace is registered — it says nothing about whether `delete` still exists on it.
  So if iX guts the method while keeping the service (they have already done exactly
  that to `pool.snapshot.do_update` on master), `tools/compat.py` would fall through
  to `zfs.snapshot`, report the box healthy, and let the patch apply — while the
  runtime picked `pool.snapshot` and failed *every* delete, orphaning the whole tree.
  Both sides now ask the same question, and a test binds the two lists together.

- `query_filesystems()` **dropped malformed `zfs list` rows silently** — the last
  remaining silent-omission path, and a direct contradiction of this module's cardinal
  rule. It raises now. A missing `zfs` binary raised `FileNotFoundError` rather than
  `ZfsError`; also fixed.

- The snapshot retry loop **discarded the delete error** and reported every survivor
  as "(still busy?)" — naming the one cause that is benign and self-healing, and
  hiding the ones that are permanent. It keeps and reports the real error.

- The staging-failure handler could **lose the original exception** if its own cleanup
  sweep raised. An error handler must not be able to lose the error.

- **A snapshot delete that returns cleanly is not proof that anything was deleted.**
  The recursive sweep's fast path took the call's word for it and returned "no
  survivors" — so `cleanup_task` read that as a clean sweep and removed the sidecar,
  the only record the tree ever existed. Roughly 250 snapshots would have been orphaned
  on every run, with nothing left able to find them, and the backup reporting success.

  This is not a hypothetical about a well-behaved API: iX has already gutted
  `pool.snapshot.do_update` on master into a no-op whose body is commented out and
  which returns `None`. A source check still sees the `def`; a runtime check still sees
  a callable method. Only asking ZFS can tell. The sweep now confirms against ZFS, and
  where it *cannot* confirm it keeps owning the tree rather than claiming success — a
  false survivor self-heals on the next run, a lost record never does.

- `_write_sidecar` **swallowed `OSError`**. The sidecar is the only thing that survives
  a middlewared restart; failing to write it is not fatal, but it must never be
  invisible. `_read_sidecar` had the mirror bug — it conflated "there is no sidecar"
  with "I could not read the sidecar", and `cleanup_task` then took the empty branch
  and **unlinked the only record** of a tree it had failed to read.

- **A dataset from another tree, mounted inside the backup path, was omitted
  silently.** The staging plan scopes by dataset *name*, which is correct — a dataset
  with no mountpoint cannot be scoped by path at all. But ZFS lets any dataset mount
  anywhere, so one from an unrelated tree can sit inside the path:

      Tank/photos   mountpoint=/mnt/Tap/apps/photos

  It holds data inside the backed-up path, and `zfs snapshot -r Tap@…` does **not**
  cover it: recursion follows the dataset tree, not the directory tree. So there is no
  snapshot of it to stage, and no way to capture it consistently with the rest. It fell
  out of the name filter and vanished — not staged, not in `skipped`, no error, backup
  green. Stock has the same blind spot, but stock also refuses the nested config
  outright; this patch is what relaxes that guard, so the hole is this patch's to close.
  It now refuses, and names the offending datasets.

## v0.6.1 — 2026-07-13
### Fixed

- **A reboot mid-backup orphaned the entire snapshot tree, permanently.** The sidecar
  is the record of which snapshots a run pinned — and it lives in `/run`, which is
  **tmpfs**. A reboot (or a crash) between taking the recursive snapshot and cleaning
  it up destroyed that record, leaving one snapshot per descendant dataset — **250+ on
  a real pool** — with nothing left pointing at them. Nothing would ever have found
  them again.

  `gc_stale_snapshots()` is the backstop: it identifies leftovers **by name**, so it
  works when the record is gone. It runs at the start of every backup, after the
  sidecar reclaim — the recorded path stays authoritative, and the collector only ever
  mops up what the record lost.

  Because it deletes data on a *name match* — a weaker claim than a recorded fact — the
  selection is a **pure function** with the harshest tests in the suite. A snapshot is
  collected only if **all** of these hold:

  | | |
  | --- | --- |
  | name is exactly `<dataset>@<task>-<YYYYMMDDHHMMSS>` | so `cloud_backup-5` never matches `cloud_backup-50`, an `auto-*` periodic snapshot, or anything a human made |
  | it is not the current run's | parent *and* children are excluded |
  | **nothing is mounted from it** | an in-flight run pins its own snapshots — this, not the age guard, is what protects a concurrent backup |
  | it is **over an hour old** | covers the seconds-long window where a live run has snapshotted but not yet mounted |

  Verified against the real pool: of **4,728** snapshots — including **2,341** periodic
  ones — it selects exactly the orphans of the task being run, and nothing else.

## v0.6.0 — 2026-07-13
### Added

- **`release.sh` — a two-stage release process, and a barrier that enforces it.**
  A stable `vX.Y.Z` tag is now only publishable if a `vX.Y.Z-rcN` tag points at the
  **same commit**, and the release job re-runs the entire suite against that tagged
  commit before publishing. Candidates are invisible to users — `update.sh` and the
  update alert both take the newest plain `vX.Y.Z` tag — so debugging happens across
  rc1, rc2, rc3 at nobody's expense, instead of across v0.5.0, v0.5.1, v0.5.2 at
  everybody's.

      bash release.sh 0.6.0 --rc        # candidate. Invisible to users.
      bash release.sh 0.6.0 --promote   # stable. Refused unless an rc passed HERE.

  The rule is enforced in `tools/release_gate.py`, which `release.sh` runs locally
  (so you fail in 200 ms) and `.github/workflows/release.yml` runs again where it
  cannot be bypassed (so failing locally is not optional). "The candidate passed,
  then I pushed one more little fix" is refused by name — that is precisely how
  v0.5.1 happened.

- **TrueNAS compatibility is now checked, not hoped for.**
  [`tools/compat.py`](tools/compat.py) is a written-down record of everything each
  module assumes about middlewared, checked in two places:

  - **CI, daily** — against iXsystems' source at every release line *including
    `master` and the current BETA/RC*. When an unreleased TrueNAS breaks the patch
    it files a bug report automatically, so there is time to fix it before that
    version reaches anyone. It also refreshes the README's support matrix, so the
    table cannot quietly become a false promise.
  - **`apply.sh`, at every boot** — against the middleware actually installed on the
    box. **A module whose assumptions no longer hold is not applied.** Stock TrueNAS
    without a feature beats TrueNAS with a broken backup.

  It immediately found two real breaks: TrueNAS 26 (below), and a nested-snapshot bug
  that had been shipping for two releases (below).

- **The compatibility check now covers the middlewared methods the patch _calls_,**
  not only the symbols it wraps — and that gap was hiding a catastrophe.

  TrueNAS 26 **deletes `plugins/zfs_/dataset.py` and `plugins/zfs_/snapshot.py`
  outright**, taking `zfs.dataset.query`, `zfs.snapshot.query` and
  `zfs.snapshot.delete` with them (26 uses `filesystem.statfs` and `zfs.resource.*`).
  Nothing about the five `cloud_backup` files reveals that, so every other check went
  green. The patch would have applied perfectly and then **failed on the first
  backup** — or, far worse, snapshotted successfully and failed to *delete*,
  orphaning one snapshot per descendant dataset (**250 on a real pool**) on every
  single run, forever.

  This is now an assumption class of its own, so a method disappearing is a BROKEN
  verdict rather than a silent time bomb.

- **Groundwork for TrueNAS 26** (async→sync and the deleted helper — see below).
  **26 is still reported BROKEN and the nested module will not apply there**, because
  the ZFS API rewrite above is not yet ported. Porting it needs a real 26 box to
  verify against, and shipping a port nobody has run is exactly the failure this
  project exists to avoid. On 26, TrueNAS is left stock: B2/S3 keeps working, nested
  datasets are simply not covered.

### Fixed

- **A few snapshots leaked on every nested run, forever.** Found on real hardware, in
  the one place it could be: a 256-snapshot backup of `/mnt/Tap` swept 253 cleanly and
  left **3 behind** with `dataset is busy`.

  The cause is ZFS's own automount. Reading anything under
  `<dataset>/.zfs/snapshot/<snap>/` makes ZFS **automount that snapshot**, and it stays
  mounted for `zfs_expire_snapshot` seconds (**300** by default) after the last access.
  `teardown()` unmounts *our* bind mounts — but not the automount underneath — so
  `zfs destroy` refuses for exactly the datasets restic read most recently. Then
  `cleanup_task()` removed the sidecar anyway, destroying the only record that those
  snapshots existed. Nothing would ever have reclaimed them.

  Three changes, and the third is the one that makes it safe rather than merely
  unlikely:
  - `release_snapdirs()` unmounts ZFS's own `.zfs/snapshot` automounts (deepest first)
    before deleting, so the snapshots are not busy in the first place.
  - `delete_snapshot_tree()` **retries** the transient busy, and **returns the
    snapshots it could not delete** instead of swallowing them.
  - **The sidecar is now removed only on a confirmed-clean sweep** — including on the
    staging-failure path, which used to remove it *before* the caller swept. The
    asymmetry is deliberate: a sidecar left behind when the tree is already gone costs
    one no-op delete on the next run, while a sidecar removed while the tree still
    exists is unrecoverable. Survivors are reclaimed by the next run.

  **Expect the occasional straggler, and expect it to clean itself up.** On a
  256-snapshot tree this reliably sweeps ~255 immediately and may leave **one**: it is
  whatever restic read last, so its 300-second window has barely opened. That one is
  logged, its sidecar is kept, and the next run reclaims it before doing anything else.
  The leak is bounded at a single cycle rather than growing without limit — which is
  the property that actually matters. Blocking a backup job for five minutes to chase
  the last snapshot would be a worse trade, so it is not made.

- **Installing the patch permanently blocked updating it.** `install.sh` does
  `chmod +x update.sh`, and git recorded `update.sh` as `100644` — so the chmod was a
  *tracked modification*, and `update.sh` refuses to run over a dirty tree. Install
  once and you could never update again; the error even told you to run
  `git checkout -- .`, which just undoes the exec bit so the next install can re-dirty
  it. A real box sat on an old version for exactly this reason.

  Fixed on both sides: the scripts `install.sh` chmods are now executable in git (so
  the chmod is a no-op), and `update.sh`'s dirty check now looks at **content**, not
  file mode — `git diff --numstat` reports `0 0` for a mode-only change. A test
  asserts every script in `install.sh`'s chmod loop is already `100755` in git.

- **Nested snapshots were broken on TrueNAS 24.10 and 25.04, and had been all
  along.** `SYNC_BLOCK`'s wrapper spelled out the stock signature and forwarded five
  arguments — but those releases declare `restic_backup(middleware, job,
  cloud_backup, dry_run)`; `rate_limit` only arrived in 25.10. Every nested backup on
  24.10/25.04 raised `TypeError: restic_backup() takes 4 positional arguments but 5
  were given`. The wrapper now takes `*args, **kwargs` and forwards whatever it is
  handed, so a trailing parameter appearing or disappearing is a non-event.

  Found by the new compatibility check, not by a user — which is the whole argument
  for having it. The check it replaced only asked whether the parameter *names* still
  appeared somewhere in the signature, so it happily passed a call that could never
  work.

- **The nested module is now one synchronous implementation behind two thin
  wrappers.** TrueNAS 26 rewrites `cloud_backup` from async to **synchronous** and
  separately **deletes `get_dataset_recursive()`**, which an injected block called out
  of the host module's namespace. Either alone is a broken backup found at restore
  time: an `async def` wrapper hands `sync.py` a coroutine where it unpacks a tuple,
  and the vanished helper is a straight `NameError`.

  The module now talks to middlewared through `call_sync`, and `apply.sh` reads which
  flavour the installed middleware declares and injects the matching wrapper —
  TrueNAS ≤ 25.10 reaches it via `await middleware.run_in_thread(...)`; a synchronous
  TrueNAS, already in a worker thread, calls it directly. The logic that owns the
  snapshots, the bind mounts and the failure modes exists **once**; an async twin
  would mean every future fix had to land twice, and the one that got missed would be
  the one that eats a backup. A middleware whose three wrapped functions **disagree**
  about async-ness is refused outright rather than guessed at, and
  `get_dataset_recursive` is carried as our own copy — removing the dependency on both
  versions instead of asserting it.

- **The patch no longer reaches into CloudSync tasks it has no business touching.**
  `create_snapshot` is module-global in `plugins/cloud/snapshot.py` and is imported by
  **`cloud_sync.py` as well as `cloud_backup/sync.py`** — so the wrapper sat in the
  path of every rclone/Storj **CloudSync** task with `snapshot=true`, and issued a
  `zfs.dataset.query` before deciding it had nothing to do. That added a brand-new
  failure mode to jobs that worked fine before this patch was installed, and worse: a
  CloudSync task that ever *did* get staged would **never be torn down**, because the
  teardown is wired into `cloud_backup`'s `restic_backup` and `CRUD_BLOCK`
  deliberately leaves CloudSync's nesting guard intact — the bind mounts would pin the
  ZFS snapshot forever. The staging path now bails out immediately unless the snapshot
  is named `cloud_backup-*`, before any middleware call.

- **Teardown warnings are no longer silently swallowed on TrueNAS ≤ 25.10.** The async
  wrapper's `finally` dropped the `logger=` kwarg that the sync one passes, so a
  cleanup that failed to unmount a bind mount *or* to delete a snapshot tree logged
  **nothing at all** — on the only platform anyone actually runs. `run_in_thread`
  forwards `**kwargs` via `functools.partial`; it was a regression, not a limitation.

- **`do_delete` is recognised as `delete`.** TrueNAS 24.10 and 25.04 declare
  `do_delete` (the `CRUDService` convention); 25.10 renamed it to `delete`. Both
  answer to `zfs.snapshot.delete`. Accepting only the literal name reported both older
  releases as BROKEN — a false verdict that would have switched nested snapshots off
  on boxes where they work perfectly.

- **An incompatible TrueNAS no longer sets the permanent kill switch.** `apply.sh`
  reused a "nothing left to do" exit that touches `disabled`, which suppresses
  patching on every future boot and is cleared only by `install.sh` — never by
  `update.sh`. On TrueNAS 26 (providers-compatible, nested opt-out by default) that
  branch would have fired, and the very release that fixed 26 could not have
  re-enabled itself: the user would run `bash update.sh`, exactly as the update alert
  tells them to, and the patch would stay dead with their B2 backups off.
  Incompatibility now means "apply nothing this boot, try again next boot".
  Retirement and incompatibility are opposite situations and no longer share an exit.

- **The compatibility check itself could be fooled**, in ways that each had teeth: a
  reordered, keyword-only, or newly-required parameter now reads as broken (the patch
  calls these positionally); a **re-exported or conditionally-defined** symbol reads
  as *unknown* rather than broken, so an innocent upstream refactor cannot make a
  working module decline to apply; an **unreadable** source (rate limit, DNS, timeout)
  is *unknown* rather than "iXsystems deleted this file", so a network blip cannot
  file a bug report, fail CI, and repaint the published support matrix; and `native`
  no longer masks `BROKEN`, which used to render a TrueNAS that both reworded the
  nesting guard *and* reshaped the functions as good news.

- **`compat.py --tree` no longer reads the patch's own code as native support.**
  `B2_BLOCK` writes `B2RcloneRemote.restic = True` into `b2.py` — exactly the string
  the providers native-probe looks for — so the one command the docs recommend for
  checking a live box said "retire the providers module" on every *patched* machine.
  It now reads only the part of the file iXsystems wrote.

- **`release.sh --promote` could never succeed.** It refused to run if the stable tag
  existed, and the gate refused if it did not — mutually exclusive, so the only way to
  cut a stable release was to hand-tag and bypass every gate this work exists to
  enforce. The gate now resolves the tag's commit if it exists and `HEAD` otherwise.
  The tests hid it by always tagging first.

### Changed

- **The minimum supported TrueNAS is stated, and enforced: 24.10.** TrueCloud Backup
  does not exist before it — `plugins/cloud_backup/` is simply absent — so the patch
  had nothing to attach to and would have done nothing at all, silently, while the
  user believed their backups were configured. `install.sh` now reads
  `system.version` and refuses, naming the reason. A version it cannot *parse* is a
  warning, not a refusal: declining to install over a string we failed to read would
  be a worse failure than the one being prevented.

- **A stable release may not leave work stranded under `## Unreleased`.** Either it
  is finished and belongs in the release, or the release is premature. Candidates
  are exempt: an rc may legitimately have work queued behind it.

- **`release.sh` refuses to run on an installed box.** The whole repo is cloned onto
  every box, so this file is there too; `update.sh` pins the checkout to a tag in
  detached HEAD, and `release.sh` now recognises that and says so, rather than
  emitting a confusing branch error.

- **Gitea (`git.onetick.ninja/flan/truenas-truecloud-patch`) is now canonical**, with
  GitHub as a mirror. Both forges run the same workflows and publish the same
  releases. The update alert now **derives the changelog URL from the `origin`
  remote** instead of hard-coding GitHub — which matters more than it sounds: when
  the changelog cannot be read, the alert deliberately fires *anyway* rather than risk
  hiding a security fix, so a stale URL would not have disabled the alert, it would
  have made it nag on every release, including documentation-only ones.

### Security

- **Workflow expressions are no longer interpolated into shell.**
  `echo "${{ steps.report.outputs.body }}"` pasted the compatibility report into the
  script text, and the report is full of backticks — bash ran `create-snapshot`,
  `def` and `async` as commands. Since that report is built from iXsystems' source,
  anything landing in their tree would have executed on the runner. `inputs.tag` on
  `workflow_dispatch` had the same shape, and that one is attacker-chosen. Data now
  moves through files and scalars through `env:`; a test enforces it across every
  workflow.

### Internal

- Static-analysis annotations in `patch/alert_source.py` (`# noqa` placement). No
  runtime change.

## v0.5.1 — 2026-07-13

### Fixed

- **The update alert could have broken middlewared at startup.** middlewared's
  `alert.load()` imports every file in `alert/source/` with **no try/except**, and
  it runs during setup — so a module that raises on import takes middlewared down
  with it. `apply.sh` now **compiles the substituted alert source and refuses to
  write it** if it does not parse. An uninstalled alert is a missing convenience;
  a broken one is a broken box.

- **`@PATCH_DIR@` is substituted with `repr()`**, so a repository path containing
  a quote or a backslash produces a valid Python literal instead of a syntax error
  in the installed module.

- **The alert source no longer mutates `sys.path`.** It loaded
  `tools/release_notes.py` via `sys.path.insert(0, …)`, which shadows the stdlib
  for that interpreter — and `ThreadedAlertSource` runs in middlewared's thread
  pool, so mutating `sys.path` is a race. It now loads the module by file path with
  `importlib`.

### Notes

Timing, for the record: `process_alerts` is `@periodic(60)` and
`alert_source_last_run` is in-memory, so the check runs **within 60 seconds of any
middlewared restart** (which this patch performs at every boot) and otherwise
**within 24 hours** of a release.

## v0.5.0 — 2026-07-13

### Added

- **A TrueNAS alert when an update is available** — the bell in the UI, not a log
  line nobody reads. On by default, checked once a day.
  `install.sh --no-update-alerts` turns it off.

  **It does not nag.** A release whose CHANGELOG contains only a `### Docs`
  section changed no code and raises nothing. Anything else raises INFO; a
  `### Security` section raises WARNING. The CHANGELOG's own section headings are
  the signal, and a security fix anywhere in the range escalates the whole span —
  so a docs-only release sitting on top of a security fix still reports as
  security, rather than hiding it.

  **Why an AlertSource and not `midclt`:** TrueNAS cannot raise an alert from the
  CLI. `midclt` exposes only `alert.dismiss`, `alert.list`, `alert.list_categories`,
  `alert.list_policies` and `alert.restore` — alert *creation* is internal to
  middlewared, and none of its ~60 one-shot classes is generic enough to reuse. So
  registering an `AlertSource` is the only way, and it is also the least invasive
  thing this patch does: it **adds one file and modifies none**, where the
  providers and nested modules both append code to stock middleware files. It is
  the native mechanism, and TrueNAS polls it itself — no cron, no systemd timer.

  - Fail-safe: every error path returns `None`; it cannot take middlewared down.
  - Read-only: `git ls-remote` plus an HTTPS fetch of the CHANGELOG. It never
    writes to `.git`, so it cannot leave root-owned objects behind the way a
    `git fetch` from middlewared (running as root) would.
  - Removed by `uninstall.sh`.
  - It only *tells* you; it never updates anything.

## v0.4.2 — 2026-07-13

### Docs

- **The Updating section never said how to *get* `update.sh`.** It ships inside the
  patch, so a clone older than v0.4.0 doesn't have it — the docs told you to run a
  script you didn't have. There is now an explicit bootstrap step (`git pull &&
  bash install.sh`, once), including the fix for the *"insufficient permission for
  adding an object to repository database"* failure that past `sudo git pull`s
  cause.

- **`After a TrueNAS update` rewritten.** It didn't explain that the patch
  re-applies itself at every boot (so you never reinstall), and it didn't say what
  each failure actually costs you. "Fail-safe" means *the box stays up* — not that
  your backups keep running. A `[FAIL] providers` is a **broken backup**, and the
  docs now say so rather than implying everything degrades gracefully.

- Added a repo map. `patch/mw_patch.py` and `tools/release_notes.py` were
  documented nowhere.

- `Development` told you to run `ruff check patch tests`, which misses `tools/`.

- Every command and file path in the README is now verified to exist and run.

## v0.4.1 — 2026-07-13

### Fixed

- **`update.sh` would have picked a release candidate as "the newest release".**
  Git's version sort ranks `v0.5.0-rc1` *above* `v0.5.0` (verified), and the
  release workflow deliberately supports rc/beta tags — so an RC would have been
  installed as though it were the latest stable. Tag selection is now filtered to
  plain `vX.Y.Z`.

- **`update.sh` would have died mid-update on an untracked file.** The dirty-tree
  guard uses `--untracked-files=no`, so an untracked file that the *target* tracks
  slipped past it — and `git checkout` then aborts. Under `set -e` the script died
  with a raw git error, *after* recording the rollback point. This is exactly what
  blocked a pull on a real box (a hand-copied `patch/wait_restart.sh`). It now
  detects the collision up front and names the files. Gitignored files are
  correctly *not* treated as blockers — git overwrites those silently.

  Special case: if `update.sh` *itself* is the blocker, you hand-copied it in to
  bootstrap — and "delete `update.sh`, then re-run `update.sh`" is impossible. It
  now says so and prints the git commands that bootstrap it properly.

- **`--rollback` skipped that check entirely**, so it would have hit the identical
  failure. The check is now a shared function used by both paths, and rollback also
  validates that the recorded revision still exists (history can be rewritten).

- `install.sh`'s `chmod` aborted under `set -e` if any listed file was missing. The
  file set changes between versions, so `update.sh --rollback` to an older revision
  must not be killed by a filename this version happens to know about.

- `--to` with no value was silently ignored and fell back to the default target.

## v0.4.0 — 2026-07-13

### Added

- **`update.sh`** — fetch a newer release and apply it, preserving your
  nested-snapshot opt-in setting.

  ```bash
  bash update.sh              # to the newest release, with a confirmation
  bash update.sh --check      # show what would happen; change nothing
  bash update.sh --rollback   # undo the last update
  ```

  **Run it by hand. Never from cron or a systemd timer.** This patch injects
  Python into middlewared and re-applies itself at every boot, so an unattended
  pull would let any bad upstream commit reach your box with no human in the loop
  and take effect on the next reboot. v0.0.4 shipped exactly such a bug and took
  every app on the box down. The manual step *is* the safety gate.

  Design:

  - **Defaults to the newest release tag, not `main`.** `main` can be mid-refactor;
    a tag is the tested artifact. `--main` exists but says so loudly.
  - Tags are ordered by **version**, not by date — date order silently downgrades
    the box the first time a hotfix is tagged out of band (a v0.3.6 released after
    v0.4.0 would sort as "newest").
  - **Refuses to run over a dirty working tree** rather than merging across
    hand-edited or scp'd files.
  - Shows the commits you don't have and the target's release notes (read from the
    *target's* CHANGELOG, via `tools/release_notes.py` — not a second copy of the
    extractor), then asks before doing anything.
  - **Records the previous revision before moving**, so `--rollback` works even if
    `install.sh` dies halfway.
  - Repairs `.git` ownership, which past `sudo git pull`s leave root-owned and
    which then breaks every later non-root git command.

- `update.sh` is covered by the version-drift check, so it cannot quietly go stale
  the way `create_task.py.__version__` did.

## v0.3.5 — 2026-07-13

### Changed

- `delete_snapshot_tree` swallowed the error from its recursive-delete fast path.
  That failure is *usually* just "parent already gone" — stock's `finally` winning
  the race once our mounts are released, which the by-name sweep then handles. But
  if the cause were anything else, this was the only place it was visible, and it
  went straight to `/dev/null`. It is now logged before falling through.

- Annotated the two remaining static-analysis findings as considered-and-accepted
  rather than leaving them to be re-litigated: `subprocess` is always called in
  list form (no shell, so ZFS dataset names cannot inject), and the partial
  `systemctl` path is moot in a script that only runs as root.

## v0.3.4 — 2026-07-13

### Changed

- **One implementation of apply/revert (`patch/mw_patch.py`).** The "strip the
  `TRUECLOUD_PATCH` block" logic existed twice — in `apply.sh`'s heredoc and in an
  inline heredoc in `uninstall.sh` — and the uninstall copy was the untested one.
  That is exactly how the two could have drifted apart, with `apply.sh` reverting
  one set of files and `uninstall.sh` another. Both now call the same tested
  module (17 new tests, including that `revert_nested` never touches `restic.py`,
  which belongs to the providers module and whose removal would silently break B2
  backups).

  `apply.sh` imports it fail-safe: if it cannot, the backend patch is skipped and
  middlewared starts stock, which is this script's whole design principle. The
  import uses `sys.path.append`, never `insert(0)` — prepending would give
  `patch/` precedence over the stdlib for that interpreter, so a future
  `patch/json.py` would shadow the real `json` and break the boot.

### Docs

- The README's `create_task.py` example still taught `--password <secret>`, which
  is how a security fix quietly fails to land. It now shows `--password-stdin`.

## v0.3.3 — 2026-07-13

### Security

- **The restic repository password no longer passes through a process's argv.**
  `create_task.py` shelled out to `midclt call cloud_backup.create '<json>'`, and
  that JSON contains the repo password — so it appeared in the process's argv,
  which is world-readable via `ps`, for the duration of the call. That password is
  the encryption key for the entire cloud backup repository.

  It now talks to the middleware through `truenas_api_client` (the library that
  backs `midclt` itself), so the password never leaves the process's memory.

- **`--password` no longer required.** Passing a secret as a CLI argument writes it
  to shell history permanently. `--password-stdin` reads it from stdin, and with
  neither flag the tool prompts via `getpass`. `--password` still works but now
  warns.

### Fixed

- **`uninstall.sh` could leave every patch installed.** It reverted by unmounting
  the overlay — but `apply.sh` only mounts one when the target directory is
  read-only. On a writable `/usr` it patches the real files in place, and uninstall
  would remove the boot hook, report success, and leave the patch applied. It now
  strips the appended blocks from the middleware files explicitly.

- **`create_task.py.__version__` had been stuck at `0.2.0`** for three releases.
  The version-drift check added in v0.3.1 only looked at `VERSION=` in shell
  scripts, so it missed the one file that actually shows a version to users
  (`--version`). The check now covers `__version__` too — and caught this
  immediately.

## v0.3.2 — 2026-07-13

### Fixed

- **`install.sh --disable-nested-snapshots` did not actually disable anything
  until the next reboot.** `apply.sh` only ever *added* patches — there was no
  revert path. Disabling removed the opt-in marker and then merely *skipped*
  re-applying, but the overlay persists for the whole boot, so the previously
  patched `plugins/cloud/{snapshot,crud}.py`, `plugins/cloud_backup/sync.py` and
  `_truecloud_nested.py` were all still sitting there — and middlewared
  re-imported them on the restart `install.sh` performs.

  It printed *"DISABLED (stock guard restored)"* while the feature kept running.
  Someone turning it off *because they were worried about it* would have believed
  it was off.

  `apply.sh` now actively reverts: it removes the module first (every injected
  block is guarded by `if _tc_nested is not None`, so the stock guard is restored
  even if a later step fails), then strips its appended blocks from the three
  patched files. `restic.py` also carries a `TRUECLOUD_PATCH` block but belongs to
  the *providers* module and is deliberately left alone — reverting it would break
  B2 backups. `install.sh --disable` also tears down any staging tree first, since
  those bind mounts pin ZFS snapshots that could otherwise never be destroyed.

  Updating **without** the flag was always correct and is unchanged: the nested
  module is never installed into middleware unless it is explicitly enabled.

## v0.3.1 — 2026-07-13

### Added

- **Automated releases.** Pushing a `v*` tag runs the full test suite and then
  cuts a GitHub release whose body is the matching `CHANGELOG.md` section — so
  release notes have exactly one source of truth, and no second place to go stale.
  The workflow refuses to publish if the tests fail, if the tag does not match the
  `VERSION=` declared by every script, or if the CHANGELOG has no section for it.

- **Version-drift check.** `VERSION=` had silently diverged to three different
  values across `install.sh`, `uninstall.sh`, `recover.sh`, and `patch/apply.sh`,
  and nothing noticed. CI now asserts every script agrees with the others and with
  the newest CHANGELOG entry.

### Note

- Releases for `v0.2.0` and `v0.2.1` were backfilled — they had been tagged but
  never released, so the releases page jumped v0.1.0 → v0.3.0 and hid the fix for
  the boot race that took every app down.

## v0.3.0 — 2026-07-13

### Added

- **`snapshot = true` now works on datasets that have child datasets** —
  **opt-in, off by default** (`install.sh --enable-nested-snapshots` /
  `--disable-nested-snapshots`). It changes how backups read their source data,
  so it is never enabled implicitly; with neither flag `install.sh` preserves
  the existing setting, so a `git pull && bash install.sh` cannot silently flip
  it. When disabled, `apply.sh` skips the patch entirely and the stock guard
  remains. `uninstall.sh` tears down any staging mounts and removes the marker.
  Stock TrueNAS refuses this with *"This option is only available for datasets
  that have no further nesting"*, which makes the snapshot option unusable for
  the single most common case on any box running Apps — every app is its own
  dataset, often with `config`/`pgdata` children of its own. Without it, the
  backup reads **live** files: databases are captured mid-write, and a busy app
  rewriting its files can stall a backup indefinitely as restic chases a moving
  target.

  The stock guard is **correct, and it is not an arbitrary limit.**
  `plugins/cloud/snapshot.py` already takes a *recursive* ZFS snapshot, but it
  then points the backup tool at the **parent** dataset's
  `.zfs/snapshot/<snap>/` directory — and ZFS does not expose child datasets
  through a parent's snapshot directory:

  ```
  /mnt/Tap/.zfs/snapshot/<snap>/apps/               -> 0 entries (children invisible)
  /mnt/Tap/apps/lidarr/config/.zfs/snapshot/<snap>/ -> the real data
  ```

  So without the guard the backup tool would walk a near-empty tree, report
  SUCCESS, and upload almost nothing. iX gate the config rather than ship a
  backup that lies about succeeding.

  This release implements the missing half. After the (already recursive)
  snapshot is taken, every descendant dataset's own `.zfs/snapshot/<snap>` is
  bind-mounted into a **staging tree** mirroring the original layout, and the
  backup tool is pointed at the staging root — a complete, consistent,
  point-in-time view of the whole subtree. Only then is the guard relaxed.

  Safety properties, in order of importance:

  - **Staging failure is loud.** If any descendant cannot be staged, the backup
    fails. A silently-incomplete backup is the exact outcome the stock guard
    exists to prevent, and it would be worse than not having the feature.
  - **A post-mount verification pass** asserts every planned target is really a
    mountpoint and the staging root is non-empty, so this can never regress into
    the empty-backup failure it is meant to fix.
  - **The guard is relaxed last.** `apply.sh` installs the traversal, patches
    `snapshot.py`, then `sync.py`, and only then `crud.py`. A partial failure
    leaves the guard intact and the option merely unavailable — never
    "guard removed, traversal missing".
  - **The patch owns the whole snapshot lifecycle.** `zfs.snapshot.delete`
    defaults to `recursive=False` and stock `restic_backup()` calls it with no
    options. Stock gets away with that only because its validation means
    `recursive` is never True in the field — but enabling nested datasets makes
    recursive snapshots real, so the parent now has one child snapshot per
    descendant dataset (160+ on a typical Apps pool). Relying on stock's delete
    would therefore orphan every child snapshot **on every successful run**.
    This patch sweeps the parent *and* all children, is idempotent against
    stock's `finally` winning the race, records the snapshot in a sidecar file
    (so a middlewared restart mid-backup cannot orphan it), reclaims the tree
    left by a crashed run, and deletes the tree when staging fails — where
    sync.py's own `finally` would otherwise delete nothing at all, because its
    `snapshot` local never gets assigned.
  - **The dataset list is enumerated *after* the snapshot, never before.** A
    list read beforehand can miss a dataset created in the gap: the recursive
    snapshot would capture it but the staging plan would not, silently omitting
    its data. Read afterwards, an unsnapshotted dataset trips the staging check
    and fails the run loudly instead.
  - **Every injected block no-ops** if `_truecloud_nested` is absent.
  - Datasets that cannot contribute to a file tree (`mountpoint=none|legacy`,
    unmounted/locked, encrypted-and-locked) are skipped and **reported** —
    never dropped silently.
  - Scoped to `cloud_backup` only. Cloud Sync (rclone) shares the same
    validation mixin but has no staging teardown wired in, so its guard is left
    in place deliberately.

  Side benefit: the staging root is a **stable** path per task, so restic can
  find its parent snapshot between runs. Stock's
  `.zfs/snapshot/<name>-<timestamp>/` path changes every run, which defeats
  restic's parent detection and forces a full re-scan each time.

- **CI** (GitHub Actions): shellcheck + `bash -n` on every script, ruff, and
  pytest on Python 3.11/3.12/3.13. Includes tests that `compile()` the
  `*_BLOCK` strings — they are Python source appended to live middlewared
  modules, so a syntax error there would break the box at boot, and nothing
  previously checked them.

### Changed

- **The patch is now two independent modules, and each retires on its own.**
  Previously the native-support check looked only for native B2 restic support
  and, on finding it, set the kill switch and disabled *everything*. With a
  second capability in the patch that would silently take a still-needed module
  down with the superseded one — TrueNAS is likely to ship one of these long
  before the other.

  `apply.sh` now detects each separately (`providers`: does `B2RcloneRemote`
  carry a real `get_restic_config()`; `nested`: is the *"no further nesting"*
  validation still in `plugins/cloud/crud.py`), skips just the superseded one,
  and only sets the kill switch once **both** are done. The UI patch belongs to
  `providers` and is skipped with it. The deferred middlewared restart now fires
  when *any* still-needed module landed — keying it off `providers` alone would
  have left a freshly-patched `nested` module on disk and never loaded on a
  native-B2 box. `hook_status.json` reports each module with an `active` flag and
  a reason.

- README rewritten to be less alarmist: dropped the warning boxes and the
  disclaimer's fear-bulleting in favour of plain statements, and documented the
  two-module design. The one caveat kept as a plain sentence: the `mount --bind`
  staging step has not yet been exercised by a live backup run.

- Version strings in `install.sh`, `uninstall.sh`, and `recover.sh` were stale
  at `0.0.4`; all scripts now report the same version.
- `patch_ui.py`: replaced a `try`/`except`/`pass` with `contextlib.suppress`
  (no behaviour change; satisfies the new lint gate).

### Removed

- `patch/__pycache__/create_task.cpython-314.pyc` was committed to the
  repository; it is now untracked and `__pycache__/` is gitignored.

### Fixed (post-merge audit)

- **`create_task.py verify` failed on a default install.** `hook_status.json`
  emitted a per-file entry for the nested module with `ok: false` whenever the
  feature was switched off — which is the default — so `verify` printed `[FAIL]`
  and exited 1 right after the README told users to run it. Status is now
  reported per *module* with an `active` flag, and `verify` renders an inactive
  module as `[SKIP]` rather than a failure.
- **A partial apply suppressed the middlewared restart.** The exit code
  conflated "nothing applied" with "one module applied, one failed", so a failing
  providers patch would prevent the restart that a freshly-applied nested patch
  needs — leaving it on disk and never loaded. Exit 2 now means partial, and the
  restart still fires.
- **The native-nested probe could never fire.** It scanned `crud.py` for the
  guard message, but our own injected block *quotes* that message, so once
  applied the probe would always conclude the guard was still present. It now
  reads only the stock portion of the file.
- `recover.sh` did not unmount staging trees, so an emergency recovery left bind
  mounts pinning ZFS snapshots that could then never be destroyed.
- `uninstall.sh` deleted sidecar files without reading them. A sidecar is the
  only record that an interrupted run's snapshot tree is still on disk; both
  scripts now name the snapshot (`zfs destroy -r ...`) before clearing it.

- **The native-nested probe could never detect the guard, silently disabling the
  whole module.** Stock splits the message across adjacent string literals:

  ```python
  verrors.add(f"{name}.snapshot", "This option is only available for datasets that have no further "
                                  "nesting")
  ```

  Python concatenates those at runtime — so the *errmsg* is contiguous and the
  runtime filter works — but the **source never contains the whole phrase**. The
  probe's substring search found nothing, concluded iX had removed the guard, and
  skipped the nested module as "already native". `apply.log` would report
  *"TrueNAS now handles nesting natively"* and the feature would never work.
  It fails safe (the stock guard stays, so no data is at risk) but the module was
  100% dead. The probe now strips whitespace and quotes before matching, which is
  robust to any wrapping style. Caught only by running the probe against real
  middlewared; there is now a regression test that executes apply.sh's own probe
  code against the real wrapped source.

### Changed (production audit)

- **`delete_snapshot_tree` now uses a single recursive delete.** It previously
  removed the parent and each child snapshot one at a time — 252 sequential
  middleware calls on a real pool. That is slow, but the real problem is that it
  is **not atomic**: a run killed part-way through the sweep leaves exactly the
  orphaned snapshots the function exists to prevent. It now issues one
  `zfs.snapshot.delete(..., {"recursive": True})` and falls back to the
  name-by-name sweep only when that fails (e.g. stock's `finally` already removed
  the parent, which leaves the children behind).

### Refactored

- Staging teardown had been copy-pasted into `uninstall.sh` and `recover.sh` —
  two untested shell copies of the fiddly depth-ordering and lazy-umount logic.
  Both now call `python3 patch/truecloud_nested.py cleanup`, so there is one
  implementation and it is the one under test.
- Dropped the in-memory `ACTIVE` dict. The sidecar file was already the source of
  truth; a second in-process record could only desync — and it is the
  middlewared-restart case (which empties it) that must not orphan a snapshot
  tree. One record, on disk, or none.

### Validated in production

An unattended scheduled backup of a live 252-dataset pool (`/mnt/Tap`, TrueNAS
25.10) ran through the staging tree end to end:

- 252 datasets recursively snapshotted, 173 bind mounts built and verified
- completed in **18m14s**, `SUCCESS` — the same task previously stalled at 74%
  for over 12 hours reading live files
- **zero** orphaned ZFS snapshots and **zero** stale mounts afterwards, which is
  the failure mode that would otherwise have accumulated 251 snapshots per run

### Known issues

- Stock `restic_backup()` deletes the ZFS snapshot in its own `finally`, which
  fails with `EBUSY` while the staging bind mounts pin it. It logs one benign
  `Error deleting snapshot ...` warning per run; the patch then unmounts and
  deletes the snapshot for real. The warning is expected and harmless.

## v0.2.1 — 2026-07-09

### Fixed

- **Deferred restart raced the rest of boot, leaving all apps and dashboard
  stats down.** The `truecloud-mw-restart` unit introduced in v0.0.4 relied
  on systemd ordering (`After=multi-user.target`, `After=ix-postinit.service`),
  which cannot see middlewared's *internal* boot work. Observed on 25.10.4:
  the restart fired two seconds into `ix-reporting.service`'s
  `midclt call reporting.start_service` and before the docker/apps startup
  task (created on middlewared's system-ready event) had run. Both were
  killed, and nothing retries them until the next boot — every app stayed
  down (`docker.status` FAILED, the apps dataset never mounted), netdata
  never started (no dashboard hardware stats), and the SMB middleware
  backend was left uninitialized.

  The transient unit now runs `patch/wait_restart.sh` instead of restarting
  directly: it waits for the systemd boot job queue to drain
  (`systemctl is-system-running --wait`, covering in-flight `ix-*` oneshots
  such as ix-reporting), then polls `midclt call docker.status` until the
  docker state machine leaves its transitional states, then allows a short
  grace period for middleware-internal tasks with no queryable state before
  issuing `systemctl try-restart middlewared`. The unit no longer sets
  `Type=oneshot` — a oneshot's start job stays in the very queue the script
  waits on and would deadlock on itself. All waits are bounded and fail
  open: worst case the restart still happens, just later.

  Recovery on a boot that already hit this (without rebooting):
  `midclt call reporting.start_service` and
  `midclt call docker.state.start_service true`.

## v0.2.0 — 2026-07-08

### Changed

- **`create_task.py` now uses the TrueNAS middleware via `midclt` instead of the
  deprecated `/api/v2.0` REST API**, which is removed in TrueNAS 26.04. Practical
  effects:
  - Run the script **on the TrueNAS host** — it uses the local middleware socket, so
    it no longer needs a host address or API key.
  - `--host`, `--api-key`, and `--insecure` are accepted but **ignored** (a deprecation
    note is printed); they will be removed in a future release.
  - `list-credentials` → `cloudsync.credentials.query`, `list-tasks` →
    `cloud_backup.query`, `create` → `cloud_backup.create`.
- Dropped the `ssl`/`urllib` HTTP client; no TLS certificate handling is needed anymore.

## v0.1.0 — 2026-07-08

### Added

- `create --cache-path PATH` — sets the restic cache directory on the task.
  Without a cache path, TrueNAS runs restic with `--no-cache`, which re-reads all
  repository metadata from the provider on every run and is glacially slow on
  large repos (a 564 GB dataset estimated **55 days** to a first backup). Tasks
  created without `--cache-path` now print a warning explaining the consequence.

## v0.0.4 — 2026-07-06

### Fixed

- **Backend patch inactive after every reboot.** PREINIT initshutdownscripts
  are executed by middlewared itself (`ix-preinit.service` runs
  `midclt call initshutdownscript.execute_init_tasks PREINIT`, ordered after
  `ix-zfs.service` pool import). By the time `apply.sh` patched `b2.py` and
  `restic.py` in the overlay, the running middlewared had already imported the
  stock modules and never re-imports — so S3/B2 support silently reverted on
  every reboot until something restarted middlewared. `install.sh` masked the
  bug because it restarts middlewared explicitly.

  Fix: when `apply.sh` detects it was invoked by middlewared (boot context),
  it now schedules a single detached restart via a transient systemd unit
  (`truecloud-mw-restart`, ordered after `multi-user.target` and
  `ix-postinit.service`) so the patched modules are loaded once boot settles.
  The restart is never synchronous — `apply.sh` is a child of middlewared's
  own job runner, and later `ix-*` boot units still need midclt. Manual runs
  of `apply.sh` never trigger a restart.

- **`TypeError: string indices must be integers` when creating a B2 task on
  TrueNAS 24.10 (Electric Eel)** (#1). The credential schema differs between
  releases: on 24.10 `credentials["provider"]` is the type string (`"B2"`)
  with the account/key in `credentials["attributes"]`, while 25.04+ moved
  them into a provider dict. The injected `get_restic_config` only handled
  the 25.04+ shape. It now detects the schema and reads the credentials from
  the right place on both; `create_task.py list-credentials` and `list-tasks`
  got the same treatment.

- **`create_task.py verify` false-positive after reboot.** `verify` trusted
  `hook_status.json`, which only records that the files were patched on disk —
  not that the running process loaded them. `verify` now also compares the
  middlewared main-process start time against `patched_at` and reports FAIL
  (with recovery instructions) when the process predates the patch.

### Changed

- README and script comments no longer claim PREINIT runs "before middlewared
  starts"; the boot ordering and the deferred restart are now documented.
- `recover.sh` and `uninstall.sh` cancel a still-queued deferred restart
  before performing their own, and their re-enable instructions now include
  the required `systemctl restart middlewared`.

---

## v0.0.3 — 2026-06-22

### Fixed

- **`patch/apply.sh` silently killed by the 10-second PREINIT timeout.**
  TrueNAS PREINIT initshutdownscripts have a 10-second default timeout. The
  previous apply.sh ran approximately 8 Python subprocesses (each ~1-2 s), so it
  was routinely killed mid-run. Symptoms: patches not applied after reboot, but
  re-running `bash apply.sh` manually (no timeout) always succeeded.

  Fix: `install.sh` now registers the hook with `"timeout": 120`. Existing
  installations are updated to the new timeout on the next `bash install.sh` run.

  Additionally, `patch/apply.sh` consolidates its Python subprocess invocations
  from ~8 down to 2, reducing startup overhead from ~12-16 s to ~2-4 s — well
  within the new 120-second budget.

### Changed

- `find_mw_python` in `apply.sh` no longer spawns a separate Python process to
  verify the interpreter can import `middlewared`. Verification is now implicit in
  the combined path-discovery subprocess that follows.

---

## v0.0.2 — 2026-06-19

### Fixed

- **B2 backup failing with `NotImplementedError` after a TrueNAS update.**
  The patch block's guard (`if "get_restic_config" not in B2RcloneRemote.__dict__`)
  could misfire and silently skip injecting the method — most likely when a
  TrueNAS version adds a stub that raises `NotImplementedError`, causing the dict
  check to return `False`. The guard is removed; the assignment is now
  unconditional. This is safe because the native-support kill switch already
  prevents patching when TrueNAS ships a real, working implementation.

- **Native-support check falsely triggering kill switch on stubs.**
  The check now inspects the source of any pre-existing `get_restic_config`
  before concluding that TrueNAS has shipped native B2 support. If the method
  body contains `NotImplementedError` it is treated as a stub and patching
  continues; only a method that does not raise `NotImplementedError` triggers
  the kill switch and auto-disable.

---

## v0.0.1 — 2026-06-16

Initial public release. Extends TrueNAS SCALE's TrueCloud Backup feature to
work with S3-compatible providers and native Backblaze B2 in addition to Storj,
using volatile overlayfs patching of the TrueNAS middleware that persists across
system updates via a PREINIT initshutdownscript.

### Included

- `install.sh` — registers the PREINIT boot hook and applies patches immediately
- `patch/apply.sh` — PREINIT script; mounts writable overlays, patches `b2.py`
  and `restic.py`, patches the Angular UI bundle to widen the credential dropdown
- `patch/create_task.py` — CLI to create TrueCloud Backup tasks with S3 or B2
  credentials, bypassing the Storj-only restriction in the UI
- `recover.sh` — emergency recovery; sets the kill switch and restarts middlewared
- `uninstall.sh` — full removal of the patch and PREINIT hook
- Kill switch support (`disabled` file) for safe degradation
- Auto-disable when TrueNAS ships native B2 restic support
- `hook_status.json` written on each boot for `create_task.py verify`
