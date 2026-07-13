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

    def test_async_to_sync_is_caught(self):
        # THE TrueNAS 26 change.
        r = check_files(with_(**{
            "plugins/cloud/snapshot.py":
                'def create_snapshot(middleware, path, name="x"):\n    return 1, 2\n',
        }))
        assert is_broken(r[NESTED])
        assert "async def" in r[NESTED]["problems"][0]["detail"]


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
                "def restic_backup(middleware, job, cloud_backup, dry_run=False, "
                "rate_limit=None):\n    pass\n",
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
                "    def _validate(self, app, verrors, name, data):\n"
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
