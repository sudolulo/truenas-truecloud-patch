"""Tests for mw_patch — the single implementation of apply/revert.

apply.sh and uninstall.sh both go through this. It used to be duplicated in an
untested shell heredoc, which is exactly how the two could have drifted apart:
apply.sh reverting one set of files and uninstall.sh another.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "patch"))

from mw_patch import (  # noqa: E402
    MARKER,
    NESTED_MODULE,
    NESTED_RELPATHS,
    PROVIDER_RELPATHS,
    patch_file,
    revert_all,
    revert_nested,
    unpatch_file,
)

STOCK = "import os\n\n\ndef stock():\n    return 1\n"
BLOCK = "\n# TRUECLOUD_PATCH\ninjected = 1\n"


def build_mw(root):
    """A fake middlewared tree with every file this project touches."""
    mw = os.path.join(root, "middlewared")
    for rel in NESTED_RELPATHS + PROVIDER_RELPATHS:
        path = os.path.join(mw, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(STOCK)
    with open(os.path.join(mw, *NESTED_MODULE), "w", encoding="utf-8") as fh:
        fh.write("# module\n")
    return mw


def read(mw, rel):
    with open(os.path.join(mw, *rel), encoding="utf-8") as fh:
        return fh.read()


class TestPatchFile:
    def test_appends_the_block(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text(STOCK)
        patch_file(str(p), BLOCK)
        assert MARKER in p.read_text()
        assert p.read_text().startswith("import os")

    def test_is_idempotent(self, tmp_path):
        # apply.sh runs on EVERY boot. Without truncate-then-append, repeated runs
        # would stack duplicate copies of the block into a middlewared module.
        p = tmp_path / "m.py"
        p.write_text(STOCK)
        for _ in range(5):
            patch_file(str(p), BLOCK)
        assert p.read_text().count("# TRUECLOUD_PATCH") == 1
        assert p.read_text().count("injected = 1") == 1

    def test_round_trips_back_to_stock(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text(STOCK)
        patch_file(str(p), BLOCK)
        assert unpatch_file(str(p)) is True
        assert p.read_text() == STOCK


class TestUnpatchFile:
    def test_returns_false_on_an_unpatched_file(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text(STOCK)
        assert unpatch_file(str(p)) is False
        assert p.read_text() == STOCK

    def test_returns_false_on_a_missing_file(self, tmp_path):
        assert unpatch_file(str(tmp_path / "nope.py")) is False


class TestRevertNested:
    def test_reverts_only_the_nested_files(self, tmp_path):
        mw = build_mw(str(tmp_path))
        for rel in NESTED_RELPATHS + PROVIDER_RELPATHS:
            patch_file(os.path.join(mw, *rel), BLOCK)

        reverted = revert_nested(mw)

        for rel in NESTED_RELPATHS:
            assert read(mw, rel) == STOCK, f"{rel[-1]} should be stock"
            assert rel[-1] in reverted

    def test_never_touches_the_providers_patch(self, tmp_path):
        # restic.py carries a TRUECLOUD_PATCH block too, but it belongs to the
        # providers module. Reverting it would silently break B2 backups.
        mw = build_mw(str(tmp_path))
        for rel in NESTED_RELPATHS + PROVIDER_RELPATHS:
            patch_file(os.path.join(mw, *rel), BLOCK)

        revert_nested(mw)

        for rel in PROVIDER_RELPATHS:
            assert MARKER in read(mw, rel), f"{rel[-1]} must keep its providers block"

    def test_removes_the_module(self, tmp_path):
        mw = build_mw(str(tmp_path))
        assert os.path.exists(os.path.join(mw, *NESTED_MODULE))
        reverted = revert_nested(mw)
        assert not os.path.exists(os.path.join(mw, *NESTED_MODULE))
        assert "_truecloud_nested.py" in reverted

    def test_module_is_removed_before_the_files_are_unpatched(self, tmp_path):
        # Every injected block is guarded by `if _tc_nested is not None`, so once
        # the module is gone they all no-op — the stock guard is restored even if
        # a later unpatch fails.
        mw = build_mw(str(tmp_path))
        for rel in NESTED_RELPATHS:
            patch_file(os.path.join(mw, *rel), BLOCK)
        reverted = revert_nested(mw)
        assert reverted[0] == "_truecloud_nested.py"

    def test_is_idempotent(self, tmp_path):
        mw = build_mw(str(tmp_path))
        for rel in NESTED_RELPATHS:
            patch_file(os.path.join(mw, *rel), BLOCK)
        revert_nested(mw)
        assert revert_nested(mw) == []


class TestRevertAll:
    def test_reverts_providers_and_nested(self, tmp_path):
        mw = build_mw(str(tmp_path))
        for rel in NESTED_RELPATHS + PROVIDER_RELPATHS:
            patch_file(os.path.join(mw, *rel), BLOCK)

        revert_all(mw)

        for rel in NESTED_RELPATHS + PROVIDER_RELPATHS:
            assert read(mw, rel) == STOCK, f"{rel[-1]} should be stock"
        assert not os.path.exists(os.path.join(mw, *NESTED_MODULE))

    def test_is_a_noop_on_a_stock_tree(self, tmp_path):
        mw = build_mw(str(tmp_path))
        os.unlink(os.path.join(mw, *NESTED_MODULE))
        assert revert_all(mw) == []


class TestTargetsAreDisjoint:
    def test_no_file_is_in_both_module_lists(self):
        # If restic.py ever appeared in NESTED_RELPATHS, revert_nested would break
        # B2 backups.
        assert not set(NESTED_RELPATHS) & set(PROVIDER_RELPATHS)

    @pytest.mark.parametrize("rel", PROVIDER_RELPATHS)
    def test_provider_targets_are_not_nested_targets(self, rel):
        assert rel not in NESTED_RELPATHS
