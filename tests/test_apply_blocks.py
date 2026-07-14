"""The *_BLOCK strings in apply.sh are Python source injected into middleware.

A syntax error in one of them would be appended to a live middlewared module and
break the box at boot. They are string literals, so nothing type-checks them --
these tests do.
"""

import ast
import os
import re
import textwrap

import pytest

APPLY_SH = os.path.join(os.path.dirname(__file__), "..", "patch", "apply.sh")

#: Every block that is actually injected into a middlewared module.
#:
#: The three nested blocks come in two flavours. TrueNAS <= 25.10 has an ASYNC
#: cloud_backup path; TrueNAS 26 rewrote it synchronous. apply.sh reads which one is
#: installed and injects the matching wrapper -- an `async def` on 26 would hand
#: sync.py a coroutine where it unpacks a tuple, and a plain `def` on 25.10 would
#: block the event loop. Both flavours must therefore be valid Python, always.
EXPECTED_BLOCKS = {
    "B2_BLOCK",
    "RESTIC_BLOCK",
    "SNAPSHOT_ASYNC",
    "SNAPSHOT_SYNC",
    "CRUD_ASYNC",
    "CRUD_SYNC",
    "SYNC_ASYNC",
    "SYNC_SYNC",
}

NESTED_BLOCKS = ["SNAPSHOT_ASYNC", "SNAPSHOT_SYNC", "CRUD_ASYNC", "CRUD_SYNC",
                 "SYNC_ASYNC", "SYNC_SYNC"]


def heredoc_source():
    with open(APPLY_SH, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"<< 'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert m, "could not find the PYEOF heredoc in apply.sh"
    return m.group(1)


def extract_blocks():
    """The blocks as apply.sh actually builds them.

    EVALUATED, not read off as string literals: each nested block is a CORE
    concatenated with a flavour-specific wrapper, so reading only `ast.Constant`
    would silently return nothing for them -- a green suite over blocks nobody
    checked. Assignments that need the runtime (argv, imports) simply fail to
    evaluate and are skipped.
    """
    tree = ast.parse(heredoc_source())
    ns, blocks = {}, {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = eval(  # noqa: S307 - our own shipped source, on purpose
                compile(ast.Expression(node.value), "<blocks>", "eval"), {}, ns
            )
        except Exception:
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and isinstance(value, str):
                ns[tgt.id] = value
                if tgt.id in EXPECTED_BLOCKS:
                    blocks[tgt.id] = value
    return blocks


def _nested_native_detector():
    """The REAL native-nested probe, lifted out of apply.sh.

    Extracted rather than reimplemented: a reimplementation would happily pass
    while the shipped probe stayed broken, which is precisely the bug this guards.
    """
    with open(APPLY_SH, encoding="utf-8") as fh:
        sh = fh.read()

    m = re.search(
        r"^(\s*)_drop = str\.maketrans\(.*?\n\s*if 'nofurthernesting' not in "
        r"stock_src\.translate\(_drop\):\n\s*result\['native_nested'\] = 'yes'",
        sh, re.S | re.M,
    )
    assert m, "could not find the native-nested probe in apply.sh"

    # The block lives inside a double-quoted shell string; undo bash's escaping.
    body = m.group(0)
    body = body.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
    body = textwrap.dedent(body)

    def detect(stock_src):
        ns = {"stock_src": stock_src, "result": {"native_nested": "no"}, "chr": chr}
        exec(body, ns)  # noqa: S102 - executing our own shipped code, on purpose
        return ns["result"]["native_nested"]

    return detect


def test_heredoc_itself_compiles():
    compile(heredoc_source(), "apply.sh:PYEOF", "exec")


def test_all_expected_blocks_present():
    assert set(extract_blocks()) == EXPECTED_BLOCKS


@pytest.mark.parametrize("name", sorted(EXPECTED_BLOCKS))
def test_injected_block_is_valid_python(name):
    block = extract_blocks()[name]
    compile(block, f"apply.sh:{name}", "exec")


@pytest.mark.parametrize("name", sorted(EXPECTED_BLOCKS))
def test_injected_block_carries_the_idempotency_marker(name):
    # patch_file() truncates each target file at "\n# TRUECLOUD_PATCH" before
    # re-appending, so every block must start with that marker or repeated runs
    # would stack duplicate copies into the middleware module.
    assert extract_blocks()[name].lstrip("\n").startswith("# TRUECLOUD_PATCH")


@pytest.mark.parametrize("name", NESTED_BLOCKS)
def test_nested_blocks_degrade_safely_without_the_module(name):
    # If _truecloud_nested failed to install, every nested block must no-op.
    # Critically this includes CRUD_BLOCK: relaxing the guard without the
    # traversal in place would mean silently-empty backups.
    block = extract_blocks()[name]
    assert "_tc_nested = None" in block
    assert "if _tc_nested is not None:" in block


class TestSnapshotLeak:
    """zfs.snapshot.delete is non-recursive and stock calls it with no options.

    A recursive snapshot has one child per descendant dataset (160+ here), so
    every path that creates one must also sweep the whole tree.
    """

    # The behaviour these once asserted as substrings -- the sweep, the re-raise, the
    # teardown in the finally -- is now asserted STRUCTURALLY, against the parsed
    # block: see TestTheStagingFailurePathReallyReRaises and
    # TestTheSyncBlockAlwaysTearsDown. As substring checks they were satisfied by
    # COMMENTS ("a cleanup that raises...", "cleanup_task gets logger=None"), so
    # deleting the actual `raise` and the actual cleanup call both left the suite
    # green -- reinstating a silently-empty backup and ~250 orphans per run.

    def test_the_snapshot_block_still_owns_the_snapshot_when_not_staging(self):
        # The TrueNAS 26 zvol/legacy orphan: stock decides `recursive` by its own rule
        # (path == mountpoint) and deletes only the parent, so we must record the
        # snapshot even on the path where we stage nothing.
        for name in ("SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"):
            stage = functions(tree_of(name), "_tc_stage")[0]
            assert calls_to(stage, "_tc_nested.own_snapshot"), (
                f"{name} hands an unstaged snapshot back to stock, whose delete is "
                f"non-recursive -- every zvol/legacy child is orphaned, every run"
            )

    def test_the_staging_plan_is_enumerated_from_ZFS(self):
        for name in ("SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"):
            stage = functions(tree_of(name), "_tc_stage")[0]
            assert calls_to(stage, "_tc_nested.query_filesystems"), (
                "the staging plan must come from query_filesystems() (which reads ZFS "
                "unfiltered); middleware's query hides ix-apps/*, .system/*, .ix-virt/*"
            )
            assert not calls_to(stage, "middleware.call_sync"), (
                "the block calls middleware directly again -- its dataset/snapshot "
                "queries are FILTERED and silently omit 84 of 270 datasets"
            )

    def test_the_vendored_helper_is_used_not_the_host_module(self):
        # TrueNAS 26 DELETED get_dataset_recursive from plugins/cloud/snapshot.py, so
        # calling it out of the host module's namespace is a NameError there.
        for name in ("SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"):
            stage = functions(tree_of(name), "_tc_stage")[0]
            assert calls_to(stage, "_tc_nested.get_dataset_recursive"), (
                "must call OUR vendored copy: TrueNAS 26 deleted the host's"
            )

    def test_datasets_are_enumerated_AFTER_the_snapshot(self):
        # A dataset created between the listing and the snapshot would be captured by
        # the recursive snapshot but missing from the staging plan -- silently omitted.
        # Read afterwards, it instead trips plan_staging's probe and fails loudly.
        for name in ("SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"):
            src = extract_blocks()[name]
            code = "\n".join(
                ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
            )
            # _tc_stage receives `snapshot` as a parameter -- i.e. it is taken by the
            # caller, before any of this runs. If the enumeration ever moves ahead of
            # create_snapshot it can only do so by leaving _tc_stage.
            assert "def _tc_stage(middleware, path, name, snapshot, snap_path)" in code
            assert "query_filesystems" in code


def test_crud_block_is_scoped_to_cloud_backup():
    # cloudsync has no staging teardown wired in, so its guard must stay.
    for name in ("CRUD_ASYNC", "CRUD_SYNC"):
        assert '!= "cloud_backup"' in extract_blocks()[name]


class TestIndependentModules:
    """The two modules must retire independently.

    TrueNAS may ship native B2 support long before (or after) it handles nested
    datasets. A single all-or-nothing kill switch would silently take a
    still-needed module down with the superseded one.
    """

    def _sh(self):
        with open(APPLY_SH, encoding="utf-8") as fh:
            return fh.read()

    def test_native_support_is_detected_per_module(self):
        sh = self._sh()
        assert "native_b2" in sh
        assert "native_nested" in sh
        assert "no further nesting" in sh, "nested native-support probe"

    def test_kill_switch_only_when_both_modules_are_done(self):
        sh = self._sh()
        assert '[ "$_providers_needed" = "0" ] && [ "$_nested_needed" = "0" ]' in sh
        # ...and that is the only place the kill switch is actually set. (Ignore
        # comment lines, which mention the same path.)
        code = [ln for ln in sh.splitlines() if not ln.lstrip().startswith("#")]
        sets = [ln for ln in code if 'touch "$PATCH_DIR/disabled"' in ln]
        assert len(sets) == 1, f"kill switch set in {len(sets)} places"

    def test_each_module_is_gated_separately(self):
        src = heredoc_source()
        assert "if not providers_needed:" in src
        assert "elif nested_native:" in src

    def test_ui_patch_is_tied_to_the_providers_module(self):
        # The UI change widens the credential dropdown; it is meaningless once B2
        # is native, but must NOT be skipped merely because nested is off.
        sh = self._sh()
        i = sh.index("--- UI patch ---")
        assert '[ "$_providers_needed" = "0" ]' in sh[i:i + 400]

    def test_status_reports_an_inactive_module_as_ok(self):
        # `create_task.py verify` fails if any patches[*].ok is false. An opt-in
        # module that is switched off (the DEFAULT) must not report FAIL, or a
        # stock install fails verification out of the box.
        src = heredoc_source()
        assert "'ok': (not nested_needed) or nested_ok" in src
        assert "'ok': (not providers_needed) or bool(b2_ok and restic_ok)" in src
        assert "'active': nested_needed" in src

    def test_nested_native_probe_matches_the_real_wrapped_source(self):
        """Stock splits the guard message across adjacent string literals.

        Python concatenates them at runtime, so the errmsg is contiguous -- but the
        SOURCE never contains the whole phrase. A raw substring search finds
        nothing, concludes iX removed the guard, and silently skips this module
        forever. This is exactly what happened, and only a run against real
        middlewared caught it.
        """
        detect = _nested_native_detector()

        # Verbatim shape from TrueNAS plugins/cloud/crud.py.
        stock_wrapped = (
            '            verrors.add(f"{name}.snapshot", '
            '"This option is only available for datasets that have no further "\n'
            '                                            "nesting")\n'
        )
        assert detect(stock_wrapped) == "no", "guard is present; must NOT report native"

        # Same message on a single line — must also be detected.
        assert detect('verrors.add(x, "... have no further nesting")\n') == "no"

        # Single-quoted, three-way split — still the guard.
        assert detect(
            "verrors.add(x, 'This option is only available for '\n"
            "               'datasets that have no further '\n"
            "               'nesting')\n"
        ) == "no"

        # Guard genuinely gone -> native support.
        assert detect("def _validate(self):\n    pass\n") == "yes"

    def test_nested_native_probe_ignores_our_own_block(self):
        # CRUD_BLOCK quotes the guard message, so scanning the whole file would
        # find the string in our own patch and never detect native support.
        sh = self._sh()
        assert "split('\\n# TRUECLOUD_PATCH', 1)[0]" in sh
        assert "no further nesting" in extract_blocks()["CRUD_ASYNC"], (
            "if this ever stops being true, the probe comment is stale"
        )

    def test_restart_fires_when_any_needed_module_landed(self):
        # Keying the restart off providers alone would leave a freshly-patched
        # nested module on disk and never loaded on a native-B2 box.
        sh = self._sh()
        i = sh.index("--- deferred restart ---")
        tail = sh[i:]
        assert '_backend_ok' in tail
        assert '"$_b2_ok"' not in tail

    def test_partial_failure_still_schedules_the_restart(self):
        # If providers fails but nested landed (or vice versa), something new IS
        # on disk. Collapsing that into "nothing to do" would leave the module
        # that succeeded permanently unloaded.
        src = heredoc_source()
        assert "sys.exit(2 if _landed else 1)" in src
        assert "_landed = (providers_needed and b2_ok and restic_ok) or (nested_needed and nested_ok)" in src

        sh = self._sh()
        assert '_rc=$?' in sh
        assert '[ "$_rc" = "2" ]' in sh


class TestOptIn:
    """Nested-snapshot support must be opt-in and must never self-enable."""

    def test_heredoc_gates_on_the_opt_in_flag(self):
        src = heredoc_source()
        assert re.search(r"nested_enabled = sys\.argv\[\d+\] == \"1\"", src)
        assert "if not nested_enabled:" in src

    def test_apply_sh_reads_the_marker_file(self):
        with open(APPLY_SH, encoding="utf-8") as fh:
            sh = fh.read()
        assert 'if [ -f "$PATCH_DIR/nested_snapshots_enabled" ]' in sh
        assert '"$_NESTED_ENABLED"' in sh

    def test_patching_is_skipped_entirely_when_disabled(self):
        # The guard-relaxing crud.py patch must be inside the enabled branch.
        src = heredoc_source()
        gate = src.index("if not nested_needed:")
        crud = src.index("patch_file(crud_py, _crud_block)")
        assert gate < crud, "crud.py patch must sit inside the opt-in branch"

    def test_disabling_REVERTS_the_patch_rather_than_merely_skipping_it(self):
        """Skipping is not disabling.

        The overlay persists for the whole boot, so a patch applied by an earlier
        run this boot is still on disk — and middlewared re-imports it on the
        restart install.sh performs. Without an active revert,
        `--disable-nested-snapshots` reports "disabled" while the feature keeps
        running until the next reboot.
        """
        src = heredoc_source()
        # The implementation lives in patch/mw_patch.py (see test_mw_patch.py);
        # apply.sh must import and actually call it.
        assert "from mw_patch import patch_file, revert_nested" in src
        gate = src.index("if not nested_needed:")
        revert = src.index("reverted = revert_nested(")
        patch = src.index("patch_file(crud_py, _crud_block)")
        assert gate < revert < patch, "revert belongs in the not-needed branch"

    def test_import_failure_skips_the_patch_rather_than_crashing(self):
        # apply.sh runs at PREINIT. If mw_patch.py cannot be imported it must
        # degrade to "middlewared starts stock", never take the boot down.
        src = heredoc_source()
        i = src.index("from mw_patch import")
        tail = src[i:i + 400]
        assert "except ImportError" in tail
        assert "skipping backend patch" in tail


def test_guard_is_relaxed_only_after_traversal_is_installed():
    # Ordering in apply.sh is a safety property: copy module -> patch snapshot.py
    # -> patch sync.py -> patch crud.py. crud.py (which unlocks the feature) must
    # come last, so a partial failure never leaves "guard removed, traversal gone".
    src = heredoc_source()
    order = [
        src.index("shutil.copyfile(nested_src, nested_dst)"),
        src.index("patch_file(snapshot_py, _snapshot_block)"),
        src.index("patch_file(sync_path, _sync_block)"),
        src.index("patch_file(crud_py, _crud_block)"),
    ]
    assert order == sorted(order), "crud.py must be patched last"


class TestWrappersDoNotHardcodeStockArity:
    """iX changes the tail of these signatures between releases.

    SYNC_BLOCK used to spell out `(middleware, job, cloud_backup, dry_run, rate_limit)`
    and forward all five. But 24.10 and 25.04 declare only four -- `rate_limit` arrived
    in 25.10 -- so every nested backup on those two releases raised
    `TypeError: restic_backup() takes 4 positional arguments but 5 were given`.
    It shipped broken and nothing noticed, because the compat check at the time only
    asked whether the parameter NAMES still appeared somewhere in the signature.

    Forwarding *args/**kwargs makes the wrapper indifferent to a trailing parameter
    being added or dropped, which is the only part iX actually churns.
    """

    def test_restic_backup_forwards_rather_than_naming_stock_params(self):
        block = extract_blocks()["SYNC_ASYNC"]
        assert "async def restic_backup(middleware, job, cloud_backup, *args, **kwargs)" in block
        assert "_tc_orig_restic_backup(middleware, job, cloud_backup, *args, **kwargs)" in block

        # Comments stripped: the block's own commentary explains the rate_limit
        # history, and that must not be mistaken for the code re-declaring it.
        code = "\n".join(
            line for line in block.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "rate_limit" not in code, (
            "naming a trailing stock parameter re-introduces the arity bug"
        )


class TestTheTwoNativeProbesCannotDrift:
    """The split-literal trick is implemented TWICE: inline in apply.sh's probe, and
    as compat._squash. It has already caused one silent bug.

    Stock middleware writes the guard as an implicitly-concatenated literal, so the
    contiguous phrase never appears in the source. A naive search finds nothing,
    concludes iX removed the guard, and reports "native" -- which means "retire the
    module". That would disable nested snapshots on every box that depends on them.

    apply.sh (runtime, on the box) and compat.py (static, in CI) must therefore agree
    on every input, or one of them is wrong about whether to retire a module.
    """

    CASES = [
        # (crud.py source, expected native?)
        ("verrors.add('x', 'datasets that have no further nesting')", False),
        # THE case: split across adjacent literals, as stock actually writes it.
        ("verrors.add('x', 'datasets that have no further '\n"
         "                 'nesting')", False),
        ('verrors.add("x", "no further "\n              "nesting")', False),
        # Guard genuinely gone -> iX implemented it -> native.
        ("verrors.add('x', 'some other validation entirely')", True),
        ("", True),
    ]

    @pytest.mark.parametrize("src,expect_native", CASES)
    def test_both_probes_agree(self, src, expect_native):
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import compat

        shipped = _nested_native_detector()(src)
        assert (shipped == "yes") == expect_native, (
            f"apply.sh's probe says native={shipped!r} for {src!r}"
        )

        path, phrase, native_when_present = compat.NATIVE_PROBES[compat.NESTED]
        present = compat._squash(phrase) in compat._squash(src)
        static_native = (present == native_when_present)
        assert static_native == expect_native, (
            f"compat.py says native={static_native} for {src!r}"
        )


class TestOnlyOurOwnTasksAreTouched:
    """create_snapshot is module-global, and cloud_sync.py imports it too.

    plugins/cloud/snapshot.py::create_snapshot is imported by BOTH
    cloud_backup/sync.py and cloud_sync.py, so our wrapper sits in the path of every
    rclone/Storj CloudSync task with snapshot=true -- tasks this patch has no business
    touching. Two consequences, the second much worse than the first:

      * every middleware call we add is a NEW failure mode for a job that worked
        before we were installed;
      * a staged CloudSync task would NEVER be torn down. The teardown is wired into
        cloud_backup's restic_backup finally, and CRUD_BLOCK deliberately leaves
        CloudSync's nesting guard intact -- so the bind mounts would pin the ZFS
        snapshot forever.

    cloud_backup names its snapshot "cloud_backup-<id>", cloud_sync "cloud_sync-<id>",
    and stock's default is "cloud_task-onetime".
    """

    @pytest.mark.parametrize("name", ["SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"])
    def test_the_staging_path_is_gated_on_cloud_backup(self, name):
        block = extract_blocks()[name]
        assert 'if not name.startswith("cloud_backup"):' in block

    @pytest.mark.parametrize("name", ["SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"])
    def test_the_bail_out_precedes_every_middleware_call(self, name):
        # The point is to add NO new failure mode to a CloudSync task. If any
        # middleware call happened before the bail-out, we would already have broken
        # the thing we are trying not to touch.
        #
        # Checked against whichever interactions the block ACTUALLY contains, not a
        # fixed list: the dataset query moved behind `_tc_nested.query_filesystems()`
        # when it switched to the public pool.* API, and a hardcoded
        # `middleware.call_sync(` simply stopped being found -- a test that silently
        # stops testing is worse than no test.
        block = extract_blocks()[name]
        gate = block.index('if not name.startswith("cloud_backup"):')

        interactions = [
            "middleware.call_sync(",
            "_tc_nested.query_filesystems(",
            "_tc_nested.stage_nested(",
            "_tc_nested.delete_snapshot_tree(",
        ]
        present = [c for c in interactions if c in block]
        assert present, "found no middleware interaction at all -- the test is vacuous"
        for call in present:
            assert gate < block.index(call), f"{call} runs before the cloud_backup gate"


# ── structural assertions ────────────────────────────────────────────────────
#
# `assert "raise" in block` was TRUE because a COMMENT in the block says "a cleanup
# that raises would replace the original exception". `assert "cleanup_task" in block`
# was TRUE because a comment says "cleanup_task gets logger=None". Deleting the actual
# `raise`, and deleting the actual cleanup call from the `finally`, both left the suite
# green -- while reinstating, respectively, a silently-empty backup and ~250 orphaned
# snapshots per run.
#
# A test that a comment can satisfy is not a test. These parse the block and assert on
# the CODE.

def tree_of(name):
    return ast.parse(textwrap.dedent(extract_blocks()[name]))


def functions(tree, name):
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    ]


def calls_to(node, dotted):
    """Every Call in `node` whose callee renders as `dotted` (e.g. a.b.c)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            try:
                if ast.unparse(n.func) == dotted:
                    out.append(n)
            except Exception:                                    # noqa: BLE001
                pass
    return out


class TestTheStagingFailurePathReallyReRaises:
    """If staging fails and we swallow it, restic backs up the UN-STAGED path.

    That is the silently-empty backup this entire module exists to prevent: stock
    points the tool at the parent's `.zfs/snapshot/`, where child datasets are
    invisible. The exception MUST propagate.
    """

    @pytest.mark.parametrize("name", ["SNAPSHOT_ASYNC", "SNAPSHOT_SYNC"])
    def test_the_handler_sweeps_the_snapshot_and_re_raises(self, name):
        stage = functions(tree_of(name), "_tc_stage")
        assert stage, "_tc_stage is gone"

        handlers = [
            h for t in ast.walk(stage[0]) if isinstance(t, ast.Try)
            for h in t.handlers
        ]
        assert handlers, "the staging failure handler is gone"

        sweeps = any(calls_to(h, "_tc_nested.delete_snapshot_tree") for h in handlers)
        assert sweeps, (
            "a staging failure no longer sweeps the snapshot. sync.py's `snapshot` "
            "local stays None, so ITS finally deletes nothing -- the whole tree leaks "
            "on every failed run."
        )

        # A bare `raise` directly in the handler body -- not one nested inside the
        # defensive try/except that wraps the sweep.
        reraises = any(
            any(isinstance(s, ast.Raise) and s.exc is None for s in h.body)
            for h in handlers
        )
        assert reraises, (
            "the staging failure is SWALLOWED. restic then runs against the un-staged "
            "path and uploads a near-empty tree, reporting SUCCESS."
        )


class TestTheSyncBlockAlwaysTearsDown:
    """The teardown is what unmounts the staging tree and sweeps the snapshot.

    It must run on EVERY exit from restic_backup -- success, failure, or exception --
    or the bind mounts pin the snapshot and the tree is orphaned.
    """

    @pytest.mark.parametrize("name", ["SYNC_ASYNC", "SYNC_SYNC"])
    def test_cleanup_runs_in_a_finally(self, name):
        fns = functions(tree_of(name), "restic_backup")
        assert fns, "the restic_backup wrapper is gone"

        tries = [t for t in ast.walk(fns[0]) if isinstance(t, ast.Try) and t.finalbody]
        assert tries, "restic_backup no longer has a try/finally"

        cleans = any(
            "cleanup_task" in ast.unparse(stmt)
            for t in tries for stmt in t.finalbody
        )
        assert cleans, (
            "cleanup_task is not called in the finally. The staging tree is never torn "
            "down, its bind mounts pin the snapshot, and ~250 snapshots leak per run."
        )


class TestTheBlockingWorkNeverRunsOnTheEventLoop:
    """`zfs list` and `call_sync` are BLOCKING. On <=25.10 these blocks are async.

    Running them directly on middlewared's event loop stalls the whole daemon.
    """

    @pytest.mark.parametrize("name,fn", [
        ("SNAPSHOT_ASYNC", "create_snapshot"),
        ("SYNC_ASYNC", "restic_backup"),
    ])
    def test_the_async_flavour_hops_to_a_thread(self, name, fn):
        fns = functions(tree_of(name), fn)
        assert fns and isinstance(fns[0], ast.AsyncFunctionDef)
        assert calls_to(fns[0], "middleware.run_in_thread"), (
            f"{name}.{fn} does the blocking work on the asyncio event loop"
        )

    @pytest.mark.parametrize("name,fn", [
        ("SNAPSHOT_SYNC", "create_snapshot"),
        ("SYNC_SYNC", "restic_backup"),
    ])
    def test_the_sync_flavour_does_not(self, name, fn):
        # On 26 stock already runs this in the thread pool; hopping again would be
        # wrong (and there is no event loop to protect).
        fns = functions(tree_of(name), fn)
        assert fns and isinstance(fns[0], ast.FunctionDef)
        assert not calls_to(fns[0], "middleware.run_in_thread")


def test_the_flavour_mapping_is_not_inverted():
    # `_snapshot_block = SNAPSHOT_ASYNC if _flavour else SNAPSHOT_SYNC` -- inverting it
    # injects an async wrapper on 26 (a coroutine gets unpacked as a tuple) or a sync
    # one on 25.10 (the event loop blocks). Every nested backup breaks, both ways.
    with open(APPLY_SH, encoding="utf-8") as fh:
        code = " ".join(
            ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#")
        )
    code = re.sub(r"\s+", " ", code)          # the assignments are space-aligned
    for block in ("SNAPSHOT", "CRUD", "SYNC"):
        assert f"{block}_ASYNC if _flavour else {block}_SYNC" in code, (
            f"the {block} flavour mapping is missing or inverted: _flavour is True for "
            f"an ASYNC middleware, so it must select {block}_ASYNC"
        )
