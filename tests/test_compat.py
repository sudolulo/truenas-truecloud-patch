"""Tests for the middlewared compatibility manifest.

Two failure directions, and they are NOT symmetric:

  * a false **BROKEN** makes a module decline to apply on a box where it works.
    Worse, if both modules go quiet, apply.sh used to set a PERMANENT kill switch
    that only install.sh clears -- so a network blip or an innocent refactor could
    take a working box's B2 backups down until someone noticed by hand.

  * a false **OK** lets the patch inject into middleware it does not fit, which is a
    broken backup discovered at restore time.

Both are tested. The `native` verdict gets its own scrutiny because it is the most
dangerous thing this file can say -- it means "TrueNAS does this now, retire the
module" -- and it rests on nothing more than a substring match.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import compat  # noqa: E402
from compat import (  # noqa: E402
    NESTED,
    PROVIDERS,
    Unreadable,
    check,
    is_broken,
)

# A middlewared that the patch fits: TrueNAS 25.10 in miniature.
GOOD = {
    "rclone/remote/b2.py": "class B2RcloneRemote(BaseRcloneRemote):\n    pass\n",
    "plugins/cloud_backup/restic.py": (
        "class ResticConfig:\n    cmd: list\n\n"
        "def get_restic_config(cloud_backup):\n    return ResticConfig([], {})\n"
    ),
    "plugins/cloud/snapshot.py": (
        'async def create_snapshot(middleware, path, name="x"):\n    return "s", "p"\n'
    ),
    "plugins/cloud/crud.py": (
        "class CloudTaskServiceMixin:\n"
        "    async def _validate(self, app, verrors, name, data):\n"
        "        verrors.add('x', 'datasets that have no further '\n"
        "                         'nesting')\n"
    ),
    "plugins/cloud_backup/sync.py": (
        "async def restic_backup(middleware, job, cloud_backup, dry_run=False, "
        "rate_limit=None):\n    pass\n"
    ),
    # The middlewared METHODS the injected code calls. TrueNAS 26 deleted both of
    # these files, taking zfs.dataset.query / zfs.snapshot.query / zfs.snapshot.delete
    # with them -- see TestMiddlewareMethodsWeCall.
    "plugins/zfs_/dataset.py": (
        "class ZFSDataset(CRUDService):\n"
        "    class Config:\n"
        "        namespace = 'zfs.dataset'\n"
        "    def query(self, filters, options):\n        pass\n"
    ),
    "plugins/zfs_/snapshot.py": (
        "class ZFSSnapshot(CRUDService):\n"
        "    class Config:\n"
        "        namespace = 'zfs.snapshot'\n"
        "    def query(self, filters, options):\n        pass\n"
        "    def delete(self, id_, options={}):\n        pass\n"
    ),
}


def loader(files):
    def load(path):
        if path not in files:
            return None
        v = files[path]
        if isinstance(v, Exception):
            raise v
        return v
    return load


def check_files(files, modules=None):
    return check(loader(files), modules)


def with_(**overrides):
    files = dict(GOOD)
    files.update(overrides)
    return files


class TestTheBaseline:
    def test_a_good_tree_is_ok_and_not_native(self):
        r = check_files(GOOD)
        for mod in (PROVIDERS, NESTED):
            assert r[mod]["ok"], r[mod]["problems"]
            assert not r[mod]["native"]
            assert not r[mod]["unknown"]


class TestFalseOkWouldBreakBackups:
    """The patch calls the originals POSITIONALLY. A name-subset check passed all of
    these, and each is a TypeError or -- worse -- silently swapped arguments."""

    def test_reordered_parameters_are_broken(self):
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py":
                'async def create_snapshot(name, path, middleware):\n    return 1, 2\n',
        }))
        assert is_broken(r[NESTED])

    def test_a_keyword_only_conversion_is_broken(self):
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py":
                'async def create_snapshot(middleware, *, path, name="x"):\n    return 1, 2\n',
        }))
        assert is_broken(r[NESTED])

    def test_a_new_required_parameter_is_broken(self):
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py":
                'async def create_snapshot(middleware, path, name, dataset):\n    return 1, 2\n',
        }))
        assert is_broken(r[NESTED])

    def test_a_new_optional_parameter_is_fine(self):
        # The patch simply will not pass it. Refusing here would be false BROKEN.
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py":
                'async def create_snapshot(middleware, path, name="x", quiet=False):\n'
                "    return 1, 2\n",
        }))
        assert r[NESTED]["ok"], r[NESTED]["problems"]

    def test_the_master_signature_change_is_caught(self):
        # iX really did rename this on master: get_restic_config(entry, credentials).
        # RESTIC_BLOCK rebinds the module-level name to a 1-arg wrapper, so getting
        # this wrong kills EVERY TrueCloud task -- Storj included.
        r = check_files(with_(**{
            "plugins/cloud_backup/restic.py":
                "class ResticConfig:\n    cmd: list\n\n"
                "def get_restic_config(entry, credentials):\n    pass\n",
        }))
        assert is_broken(r[PROVIDERS])

    def test_a_vanished_symbol_is_broken(self):
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py": "def something_else():\n    pass\n",
        }))
        assert is_broken(r[NESTED])


class TestFalseBrokenWouldDisableWorkingBoxes:
    def test_a_conditionally_defined_symbol_is_not_broken(self):
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py":
                "try:\n"
                "    from .fast import create_snapshot\n"
                "except ImportError:\n"
                '    async def create_snapshot(middleware, path, name="x"):\n'
                "        return 1, 2\n",
        }))
        assert not is_broken(r[NESTED]), r[NESTED]["problems"]

    def test_a_re_exported_symbol_is_unknown_not_broken(self):
        r = check_files(with_(**{
            "plugins/cloud_backup/restic.py":
                "from ._impl import ResticConfig, get_restic_config\n",
        }))
        assert not is_broken(r[PROVIDERS])
        assert r[PROVIDERS]["unknown"]

    def test_an_unreadable_source_is_unknown_not_broken(self):
        # A rate limit (the matrix makes ~30 unauthenticated requests) must not be
        # able to say "iX deleted six files, both modules are broken".
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py": Unreadable("HTTP 429"),
        }))
        assert not is_broken(r[NESTED])
        assert r[NESTED]["unknown"]

    def test_a_definite_break_still_wins_over_an_unknown(self):
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py": Unreadable("HTTP 429"),
            "plugins/cloud_backup/sync.py":
                "async def restic_backup(job, middleware, cloud_backup):\n    pass\n",
        }))
        assert is_broken(r[NESTED]), "unknown must not launder away a proven break"


class TestTheNativeVerdict:
    """"native" means "retire the module". It is the most destructive thing this file
    can say, and it is only a substring match — so it must never outrank BROKEN."""

    def test_broken_outranks_native(self):
        # Guard reworded (reads as native) AND the signatures changed (really broken).
        # This used to render as good news: green CI, no bug report, and a README row
        # telling users the feature went native while it was in fact broken.
        r = check_files(with_(**{
            "plugins/cloud/crud.py":
                "class CloudTaskServiceMixin:\n"
                "    async def _validate(self, verrors, name):\n"
                "        verrors.add('x', 'no children allowed')\n",
        }))
        assert r[NESTED]["native"]
        assert is_broken(r[NESTED])
        assert compat._verdict(r[NESTED]) == "BROKEN"

    def test_an_already_patched_tree_does_not_read_as_native(self):
        # B2_BLOCK writes `B2RcloneRemote.restic = True` into b2.py. Scanning the whole
        # file finds OUR OWN line and concludes TrueNAS went native — so the command
        # compat.py's docstring recommends for a live box (`--tree /usr/lib/...`)
        # reported providers as native on every patched machine.
        r = check_files(with_(**{
            "rclone/remote/b2.py":
                "class B2RcloneRemote(BaseRcloneRemote):\n    pass\n"
                "\n# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh\n"
                "B2RcloneRemote.restic = True\n",
        }))
        assert not r[PROVIDERS]["native"], "read its own patch as native support"
        assert r[PROVIDERS]["ok"]

    def test_a_genuinely_native_b2_is_native(self):
        r = check_files(with_(**{
            "rclone/remote/b2.py":
                "class B2RcloneRemote(BaseRcloneRemote):\n    restic = True\n",
        }))
        assert r[PROVIDERS]["native"]


class TestUpdateReadmeCannotPublishAGuess:
    def test_it_refuses_when_anything_is_unknown(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text(f"x\n{compat.BEGIN}\nold\n{compat.END}\ny\n")
        rows = [{
            "ref": "TS-25.10.4", "unreleased": False,
            "modules": check_files(with_(**{
                "plugins/cloud/snapshot.py": Unreadable("HTTP 429"),
            })),
        }]
        with pytest.raises(Unreadable):
            compat.update_readme(rows, path=str(readme))
        assert "old" in readme.read_text(), "a blip must not repaint the matrix"


class TestAsyncFlavour:
    """TrueNAS <= 25.10 is async; 26 is synchronous. Both are supported -- apply.sh
    injects the wrapper that matches. So asyncness is DETECTED, never assumed."""

    def test_async_middleware_is_detected(self):
        assert compat.async_flavour(loader(GOOD)) is True

    def test_sync_middleware_is_detected(self):
        sync = dict(GOOD)
        sync["plugins/cloud/snapshot.py"] = (
            'def create_snapshot(middleware, path, name="x"):\n    return "s", "p"\n'
        )
        sync["plugins/cloud/crud.py"] = (
            "class CloudTaskServiceMixin:\n"
            "    def _validate(self, app, verrors, name, data):\n"
            "        verrors.add('x', 'no further nesting')\n"
        )
        sync["plugins/cloud_backup/sync.py"] = (
            "def restic_backup(middleware, job, cloud_backup, dry_run=False, "
            "rate_limit=None):\n    pass\n"
        )
        assert compat.async_flavour(loader(sync)) is False

    def test_a_HALF_converted_middleware_is_refused(self):
        # The dangerous middle. If iX converts create_snapshot but not restic_backup,
        # there is no single wrapper flavour that works -- and guessing means either
        # a coroutine unpacked as a tuple, or the event loop blocked. None means
        # "do not patch"; apply.sh turns that into a skip, not a guess.
        half = dict(GOOD)
        half["plugins/cloud/snapshot.py"] = (
            'def create_snapshot(middleware, path, name="x"):\n    return "s", "p"\n'
        )
        assert compat.async_flavour(loader(half)) is None

    def test_an_unreadable_source_refuses_rather_than_guesses(self):
        broken = dict(GOOD)
        broken["plugins/cloud_backup/sync.py"] = Unreadable("HTTP 429")
        assert compat.async_flavour(loader(broken)) is None

    def test_the_real_truenas_versions(self):
        # Pinning the actual fact this whole port exists for.
        assert compat.async_flavour(loader(GOOD)) is True


class TestMiddlewareMethodsWeCall:
    """The assumption class that was MISSING, and that hid a catastrophic break.

    The manifest recorded the symbols the patch WRAPS. It said nothing about the
    middlewared methods the patch CALLS -- and TrueNAS 26 deleted
    plugins/zfs_/dataset.py and plugins/zfs_/snapshot.py outright, taking
    `zfs.dataset.query`, `zfs.snapshot.query` and `zfs.snapshot.delete` with them.

    Nothing about the five cloud_backup files reveals that. The patch applied
    perfectly and every other check went green. The first backup would have failed --
    or, far worse, snapshotted fine and then failed to DELETE, orphaning one snapshot
    per descendant dataset (250 on a real pool) on every run, forever.
    """

    ZFS_SNAPSHOT = (
        "class ZFSSnapshot(CRUDService):\n"
        "    class Config:\n"
        "        namespace = 'zfs.snapshot'\n"
        "    def query(self, filters, options):\n        pass\n"
        "    def delete(self, id_, options={}):\n        pass\n"
    )
    ZFS_DATASET = (
        "class ZFSDataset(CRUDService):\n"
        "    class Config:\n"
        "        namespace = 'zfs.dataset'\n"
        "    def query(self, filters, options):\n        pass\n"
    )

    def _tree(self, **over):
        files = dict(GOOD)
        files["plugins/zfs_/snapshot.py"] = self.ZFS_SNAPSHOT
        files["plugins/zfs_/dataset.py"] = self.ZFS_DATASET
        files.update(over)
        return files

    def test_present_methods_are_ok(self):
        r = check_files(self._tree())
        assert r[NESTED]["ok"], r[NESTED]["problems"]

    def test_a_deleted_plugin_file_is_broken(self):
        # Literally TrueNAS 26: plugins/zfs_/snapshot.py does not exist.
        r = check_files(self._tree(**{"plugins/zfs_/snapshot.py": None}))
        assert is_broken(r[NESTED])
        details = " ".join(p["detail"] for p in r[NESTED]["problems"])
        assert "zfs.snapshot.delete" in details

    def test_a_renamed_namespace_is_broken(self):
        r = check_files(self._tree(**{
            "plugins/zfs_/snapshot.py": self.ZFS_SNAPSHOT.replace(
                "'zfs.snapshot'", "'zfs.resource.snapshot'"),
        }))
        assert is_broken(r[NESTED])

    def test_the_CRUDService_do_prefix_is_accepted(self):
        # 24.10 and 25.04 declare `do_delete`; 25.10 renamed it to `delete`. BOTH
        # answer to zfs.snapshot.delete. Accepting only the literal name reported the
        # two older releases as broken -- a false BROKEN that would have switched off
        # nested snapshots on boxes where they work perfectly.
        r = check_files(self._tree(**{
            "plugins/zfs_/snapshot.py": self.ZFS_SNAPSHOT.replace(
                "def delete(", "def do_delete("),
        }))
        assert r[NESTED]["ok"], r[NESTED]["problems"]

    def test_the_snapshot_delete_reason_names_the_orphan_risk(self):
        # If this ever regresses, whoever reads the bug report must understand that
        # it is not a cosmetic failure.
        r = check_files(self._tree(**{"plugins/zfs_/snapshot.py": None}))
        whys = " ".join(p["why"] for p in r[NESTED]["problems"])
        assert "orphan" in whys
