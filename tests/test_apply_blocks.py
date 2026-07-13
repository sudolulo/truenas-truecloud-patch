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

EXPECTED_BLOCKS = {
    "B2_BLOCK",
    "RESTIC_BLOCK",
    "SNAPSHOT_BLOCK",
    "CRUD_BLOCK",
    "SYNC_BLOCK",
}


def heredoc_source():
    with open(APPLY_SH, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"<< 'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert m, "could not find the PYEOF heredoc in apply.sh"
    return m.group(1)


def extract_blocks():
    tree = ast.parse(heredoc_source())
    blocks = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id.endswith("_BLOCK")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    blocks[tgt.id] = node.value.value
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


@pytest.mark.parametrize("name", ["SNAPSHOT_BLOCK", "CRUD_BLOCK", "SYNC_BLOCK"])
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

    def test_staging_failure_deletes_the_snapshot_tree(self):
        # On a staging failure, sync.py's `snapshot, local_path = await
        # create_snapshot(...)` never completes, so its local `snapshot` stays
        # None and its finally deletes nothing. We must sweep it ourselves.
        block = extract_blocks()["SNAPSHOT_BLOCK"]
        assert "except Exception:" in block
        assert "delete_snapshot_tree" in block
        assert "raise" in block

    def test_sync_block_cleans_up_on_every_path(self):
        block = extract_blocks()["SYNC_BLOCK"]
        assert "finally:" in block
        assert "cleanup_task" in block


def test_crud_block_is_scoped_to_cloud_backup():
    # cloudsync has no staging teardown wired in, so its guard must stay.
    assert '!= "cloud_backup"' in extract_blocks()["CRUD_BLOCK"]


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
        assert "no further nesting" in extract_blocks()["CRUD_BLOCK"], (
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
        crud = src.index("patch_file(crud_py, CRUD_BLOCK)")
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
        patch = src.index("patch_file(crud_py, CRUD_BLOCK)")
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
        src.index("patch_file(snapshot_py, SNAPSHOT_BLOCK)"),
        src.index("patch_file(sync_path, SYNC_BLOCK)"),
        src.index("patch_file(crud_py, CRUD_BLOCK)"),
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
        block = extract_blocks()["SYNC_BLOCK"]
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
