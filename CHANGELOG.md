# Changelog

Work lands under **Unreleased** and stays there until a release promotes it. That
is deliberate: see [Releasing](README.md#releasing). Twelve releases were cut on
2026-07-13, several of them fixing the release before — and with the update alert
live, every one of those interrupts every user. An alert people learn to ignore is
worse than no alert, because one day it carries a security fix.

## Unreleased

### Added

- **`release.sh` — a two-stage release process, and a barrier that enforces it.**
  A stable `vX.Y.Z` tag is now only publishable if a `vX.Y.Z-rcN` tag points at the
  **same commit** and that candidate's CI run passed. Candidates are invisible to
  users — `update.sh` and the update alert both take the newest plain `vX.Y.Z` tag —
  so debugging happens across rc1, rc2, rc3 at nobody's expense, instead of across
  v0.5.0, v0.5.1, v0.5.2 at everybody's.

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

### Fixed

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

- **TrueNAS 26 rewrites the entire `cloud_backup` path from async to synchronous.**
  Every block the nested module injects is an `async def` wrapping an `await`ed
  original, so on 26 it would hand `sync.py` a coroutine where it unpacks a tuple —
  a broken backup, discovered at restore time. On TrueNAS 26 the nested module now
  stays off rather than applying and breaking.

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
