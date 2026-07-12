"""The *_BLOCK strings in apply.sh are Python source injected into middleware.

A syntax error in one of them would be appended to a live middlewared module and
break the box at boot. They are string literals, so nothing type-checks them --
these tests do.
"""

import ast
import os
import re

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


class TestOptIn:
    """Nested-snapshot support must be opt-in and must never self-enable."""

    def test_heredoc_gates_on_the_opt_in_flag(self):
        src = heredoc_source()
        assert "nested_enabled = sys.argv[7]" in src
        assert "if not nested_enabled:" in src

    def test_apply_sh_reads_the_marker_file(self):
        with open(APPLY_SH, encoding="utf-8") as fh:
            sh = fh.read()
        assert 'if [ -f "$PATCH_DIR/nested_snapshots_enabled" ]' in sh
        assert '"$_NESTED_ENABLED"' in sh

    def test_patching_is_skipped_entirely_when_disabled(self):
        # The guard-relaxing crud.py patch must be inside the enabled branch.
        src = heredoc_source()
        gate = src.index("if not nested_enabled:")
        crud = src.index("patch_file(crud_py, CRUD_BLOCK)")
        assert gate < crud, "crud.py patch must sit inside the opt-in branch"


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
