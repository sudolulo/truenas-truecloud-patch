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
import compat_publish  # noqa: E402
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
    # The middlewared METHODS the injected code calls. These now go through the
    # PUBLIC pool.* API: TrueNAS 26 deleted plugins/zfs_/ outright, taking the whole
    # private zfs.* service with it -- see TestMiddlewareMethodsWeCall.
    #
    # This default tree is a MODERN box (25.10/26): it has pool.snapshot and no
    # zfs.snapshot. The older shape is built explicitly where it is tested.
    # Not a plugin: a method on the middleware OBJECT. `snapshot_service()` resolves
    # the snapshot namespace through it, so if it vanishes the module cannot sweep the
    # snapshot it just took.
    "utils/plugins.py": (
        "class LoadPluginsMixin:\n"
        "    def get_service(self, name):\n        pass\n"
    ),
    "plugins/pool_/dataset.py": (
        "class PoolDatasetService(CRUDService):\n"
        "    class Config:\n"
        "        namespace = 'pool.dataset'\n"
        "    def query(self, filters, options):\n        pass\n"
    ),
    "plugins/pool_/snapshot.py": (
        "class PoolSnapshotService(CRUDService):\n"
        "    class Config:\n"
        "        namespace = 'pool.snapshot'\n"
        "    def query(self, filters, options):\n        pass\n"
        "    def delete(self, id_, options={}):\n        pass\n"
    ),
}

#: A 24.10/25.04 box: `pool.snapshot` does not exist yet and the snapshot CRUD
#: service still answers to the (then-public) `zfs.snapshot`.
ZFS_ERA_SNAPSHOT = (
    "class ZFSSnapshot(CRUDService):\n"
    "    class Config:\n"
    "        namespace = 'zfs.snapshot'\n"
    "    def query(self, filters, options):\n        pass\n"
    "    def do_delete(self, id_, options={}):\n        pass\n"
)


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

    def test_it_reads_STOCK_source_not_our_own_injected_block(self):
        # This was a byte-identical copy of test_async_middleware_is_detected under a
        # name that promised more. The fact worth pinning: apply.sh re-runs on an
        # ALREADY-PATCHED overlay, so the probe must cut our block off first -- our own
        # SNAPSHOT_SYNC wrapper is a plain `def create_snapshot`, and reading it would
        # report a 25.10 box as synchronous and inject the wrong flavour.
        patched = dict(GOOD)
        patched["plugins/cloud/snapshot.py"] = (
            GOOD["plugins/cloud/snapshot.py"]
            + "\n# TRUECLOUD_PATCH\n"
            + 'def create_snapshot(middleware, path, name="x"):\n    return "s", "p"\n'
        )
        assert compat.async_flavour(loader(patched)) is True, (
            "the flavour probe read our own injected block and concluded the box is "
            "synchronous -- it would then inject a sync wrapper into an async "
            "middleware, and every nested backup would break"
        )


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

    POOL_SNAPSHOT = GOOD["plugins/pool_/snapshot.py"]

    def _tree(self, **over):
        files = dict(GOOD)
        files.update(over)
        return files

    #: A 26 box: pool.snapshot only.
    def _modern(self, **over):
        return self._tree(**over)

    #: A 24.10/25.04 box: zfs.snapshot only -- plugins/pool_/snapshot.py does not
    #: exist yet.
    def _zfs_era(self, **over):
        return self._tree(**{
            "plugins/pool_/snapshot.py": None,
            "plugins/zfs_/snapshot.py": ZFS_ERA_SNAPSHOT,
            **over,
        })

    def test_present_methods_are_ok(self):
        r = check_files(self._modern())
        assert r[NESTED]["ok"], r[NESTED]["problems"]

    def test_the_OLD_zfs_era_snapshot_service_also_satisfies_the_call(self):
        # 24.10 and 25.04 have no `pool.snapshot` at all -- the CRUD service is the
        # then-public `zfs.snapshot`. Pinning only the modern spelling marked both of
        # those releases BROKEN and would have switched nested snapshots OFF on boxes
        # where they work perfectly. The runtime picks the same way; see
        # pick_snapshot_service().
        r = check_files(self._zfs_era())
        assert r[NESTED]["ok"], r[NESTED]["problems"]

    def test_it_is_broken_only_when_NEITHER_namespace_exists(self):
        # The real failure: middleware drops the last spelling we know how to call.
        r = check_files(self._tree(**{
            "plugins/pool_/snapshot.py": None,
            "plugins/zfs_/snapshot.py": None,
        }))
        assert is_broken(r[NESTED])
        details = " ".join(p["detail"] for p in r[NESTED]["problems"])
        assert "pool.snapshot.delete" in details
        assert "zfs.snapshot.delete" in details, (
            "the report must say BOTH spellings were tried, or whoever reads it will "
            "think we simply never looked for the one their box has"
        )

    def test_we_do_NOT_depend_on_a_middleware_dataset_query_at_all(self):
        # iX could delete plugins/pool_/dataset.py tomorrow and the patch would not
        # care, because the staging plan is enumerated from ZFS, not from middleware.
        #
        # That is deliberate, and it was expensive to learn. `pool.dataset.query`
        # exists and is correctly shaped -- and it LIES: it applies a visibility
        # policy that hides ix-apps/*, .system/* and .ix-virt/* (84 of 270 datasets
        # on the real pool, including live app data). No source check could ever
        # have caught that; only running it could. So there is no assumption here
        # left to break.
        r = check_files(self._modern(**{"plugins/pool_/dataset.py": None}))
        assert r[NESTED]["ok"], r[NESTED]["problems"]

        ids = {c.id for c in compat.MIDDLEWARE_CALLS}
        assert not any("dataset" in i or "query" in i for i in ids), (
            "a dataset/snapshot QUERY assumption crept back into the manifest -- "
            "middleware's queries are filtered; enumerate from ZFS"
        )

    def test_a_renamed_namespace_is_broken(self):
        r = check_files(self._tree(**{
            "plugins/pool_/snapshot.py": self.POOL_SNAPSHOT.replace(
                "'pool.snapshot'", "'zfs.resource.snapshot'"),
            "plugins/zfs_/snapshot.py": None,
        }))
        assert is_broken(r[NESTED])

    def test_the_CRUDService_do_prefix_is_accepted(self):
        # A CRUDService exposes `delete` from a method NAMED `do_delete`. Both
        # spellings are live across the matrix. Accepting only the literal name
        # reported working releases as broken.
        r = check_files(self._modern(**{
            "plugins/pool_/snapshot.py": self.POOL_SNAPSHOT.replace(
                "def delete(", "def do_delete("),
        }))
        assert r[NESTED]["ok"], r[NESTED]["problems"]

    def test_the_snapshot_delete_reason_names_the_orphan_risk(self):
        # If this ever regresses, whoever reads the bug report must understand that
        # it is not a cosmetic failure.
        r = check_files(self._tree(**{
            "plugins/pool_/snapshot.py": None,
            "plugins/zfs_/snapshot.py": None,
        }))
        whys = " ".join(p["why"] for p in r[NESTED]["problems"])
        assert "orphan" in whys


class TestTheMethodCheckIsNotJustANamespaceCheck:
    """compat must verify the METHOD, not merely that the namespace still exists.

    Deleting the method check entirely used to leave all 304 tests green -- so the
    "namespace AND method" claim was unenforced and silently revertible. It is the
    half of the predicate that catches iX gutting a method while keeping its service,
    which they have already done to `pool.snapshot.do_update` on master.
    """

    def test_a_namespace_that_no_longer_defines_delete_is_broken(self):
        gutted = (
            "class PoolSnapshotService(CRUDService):\n"
            "    class Config:\n"
            "        namespace = 'pool.snapshot'\n"
            "    def query(self, filters, options):\n        pass\n"
            # do_delete is GONE -- the service is still registered and still a
            # CRUDService, so it still INHERITS a callable `delete`.
        )
        r = check_files(with_(**{
            "plugins/pool_/snapshot.py": gutted,
            "plugins/zfs_/snapshot.py": None,      # no fallback either
        }))
        assert is_broken(r[NESTED]), (
            "a namespace with no delete must be BROKEN. Checking only that the "
            "namespace exists would apply the patch to a box that cannot sweep its "
            "own snapshots."
        )

    def test_the_alternative_still_saves_it_when_only_the_primary_is_gutted(self):
        gutted = (
            "class PoolSnapshotService(CRUDService):\n"
            "    class Config:\n"
            "        namespace = 'pool.snapshot'\n"
            "    def query(self, filters, options):\n        pass\n"
        )
        r = check_files(with_(**{
            "plugins/pool_/snapshot.py": gutted,
            "plugins/zfs_/snapshot.py": ZFS_ERA_SNAPSHOT,
        }))
        assert r[NESTED]["ok"], r[NESTED]["problems"]


class TestUnreadableIsNeverOkAndNeverBroken:
    """A rate limit is not a regression, and it is not a clean bill of health either.

    compat runs ~30 unauthenticated GitHub requests per matrix; 429 is a real outcome.
    It also runs at BOOT against the installed tree, where a read can fail with EACCES.

      * treating unreadable as BROKEN repaints the README, files a bug report, and
        makes apply.sh refuse the module on a box where it works.
      * treating it as OK injects a module whose delete may be gone.

    Both mutations used to pass the whole suite.
    """

    def test_both_spellings_unreadable_is_unknown_not_broken(self):
        r = check_files(with_(**{
            "plugins/pool_/snapshot.py": Unreadable("HTTP 429"),
            "plugins/zfs_/snapshot.py": Unreadable("HTTP 429"),
        }))
        assert not is_broken(r[NESTED]), "a 429 is not iX deleting the snapshot service"
        assert r[NESTED]["unknown"]

    def test_an_unreadable_primary_with_a_healthy_alternative_is_ok(self):
        r = check_files(with_(**{
            "plugins/pool_/snapshot.py": Unreadable("HTTP 429"),
            "plugins/zfs_/snapshot.py": ZFS_ERA_SNAPSHOT,
        }))
        assert r[NESTED]["ok"], r[NESTED]["problems"]
        assert not r[NESTED]["unknown"], (
            "one spelling answered the question; the other's 429 is irrelevant"
        )

    def test_a_missing_primary_with_an_unreadable_alternative_is_unknown(self):
        # We cannot tell whether the box is broken. Saying either would be a guess.
        r = check_files(with_(**{
            "plugins/pool_/snapshot.py": None,
            "plugins/zfs_/snapshot.py": Unreadable("HTTP 429"),
        }))
        assert not is_broken(r[NESTED])
        assert r[NESTED]["unknown"]


class TestGetServiceIsChecked:
    """The runtime resolves the snapshot namespace through `middleware.get_service`.

    It is not a plugin method, so the manifest had no way to express it and never
    checked it. If it vanishes, `_can_delete` reports BOTH namespaces unusable and
    every nested backup fails -- on a box the preflight had declared healthy.
    """

    def test_a_middleware_without_get_service_is_broken(self):
        r = check_files(with_(**{"utils/plugins.py": None}))
        assert is_broken(r[NESTED])
        details = " ".join(p["detail"] for p in r[NESTED]["problems"])
        assert "get_service" in details


class TestATransientNetworkBlipDoesNotWakeAnybody:
    """The fingerprint must digest what iX BROKE, not what GitHub failed to serve.

    `unknown` problems (a 429 on one of ~30 unauthenticated fetches, an EACCES at boot)
    used to be folded into an already-broken module's problem list, so one blip flipped
    the fingerprint, `compat_publish` rewrote the issue body, and the next clean run
    rewrote it back. Daily churn is what teaches people to ignore the bot -- which is
    the whole thing this fingerprint exists to prevent.
    """

    def _rows(self, files):
        return [{"ref": "master", "modules": check_files(files)}]

    def test_an_unreadable_file_does_not_change_the_fingerprint_of_a_broken_ref(self):
        # The blip must land in the SAME module that is broken. Put it in `providers`
        # (which is healthy) and `fingerprint()` skips the whole module via
        # `is_broken(m)` -- so the `state` filter under test never runs and the test
        # passes no matter what the code does. `nested` is the broken one here, so the
        # unreadable file goes in `nested` too.
        broken = with_(**{
            "plugins/cloud/snapshot.py":
                "async def create_snapshot(name, path, middleware):\n    return 1, 2\n",
        })
        clean = compat.fingerprint(self._rows(broken))

        blipped = dict(broken)
        blipped["plugins/cloud_backup/sync.py"] = Unreadable("HTTP 429")   # nested
        assert compat.fingerprint(self._rows(blipped)) == clean, (
            "a rate-limited fetch changed the fingerprint, so the bot rewrites the "
            "issue body and then rewrites it back tomorrow"
        )

    def test_a_REAL_new_finding_still_changes_it(self):
        # ...and the anti-noise measure must not have made it deaf.
        broken = with_(**{
            "plugins/cloud/snapshot.py":
                "async def create_snapshot(name, path, middleware):\n    return 1, 2\n",
        })
        worse = dict(broken)
        worse["plugins/cloud_backup/restic.py"] = (
            "class ResticConfig:\n    cmd: list\n\n"
            "def get_restic_config(entry, credentials):\n    pass\n"
        )
        assert compat.fingerprint(self._rows(worse)) != compat.fingerprint(self._rows(broken))


class TestTheBotFindsItsOwnIssueOnBOTHForges:
    """`find_issue` decides "have I already filed this?" -- and it ran on two forges.

    It used to skip pull requests with `"pull_request" not in i`. GitHub omits that key
    on a plain issue; **Gitea sends it as `null`**. So on Gitea every issue looked like
    a PR, the match list was always empty, and the bot took the "nothing filed yet"
    branch on EVERY run: nine duplicate copies of the same report on the canonical
    forge, four of them filed after the commit that was supposed to stop exactly this.

    It is the same failure the spam fix was written to prevent, moved from comments to
    issues -- and it survived because `find_issue` was the one function here with no
    test. So the payload shapes are pinned, per forge, by hand.
    """

    TITLE = compat_publish.TITLE

    def _find(self, monkeypatch, payload):
        monkeypatch.setattr(compat_publish, "_call", lambda *a, **k: payload)
        return compat_publish.find_issue("https://forge/api", "tok", self.TITLE)

    def test_gitea_sends_pull_request_as_null_and_the_issue_is_still_found(self, monkeypatch):
        found = self._find(monkeypatch, [
            {"number": 7, "title": self.TITLE, "state": "open", "pull_request": None},
            {"number": 1, "title": self.TITLE, "state": "open", "pull_request": None},
        ])
        assert found is not None, (
            "find_issue missed a Gitea issue, so the bot files a NEW duplicate report "
            "every run -- which is how nine of them piled up"
        )
        assert found["number"] == 1, "lowest-numbered wins"

    def test_github_omits_the_key_entirely_and_the_issue_is_still_found(self, monkeypatch):
        found = self._find(monkeypatch, [
            {"number": 2, "title": self.TITLE, "state": "open"},
        ])
        assert found is not None and found["number"] == 2

    def test_a_real_PR_with_the_same_title_is_still_skipped_on_both(self, monkeypatch):
        # The reason the filter exists at all: both forges list PRs on /issues, and
        # commenting on a PR instead of the bug report would be worse than useless.
        assert self._find(monkeypatch, [
            {"number": 3, "title": self.TITLE, "state": "open",           # Gitea PR
             "pull_request": {"merged": False}},
            {"number": 4, "title": self.TITLE, "state": "open",           # GitHub PR
             "pull_request": {"url": "https://api.github.com/..."}},
        ]) is None

    def test_an_unrelated_issue_is_not_mistaken_for_the_report(self, monkeypatch):
        assert self._find(monkeypatch, [
            {"number": 1, "title": "TypeError when create B2 backup on Electric Eel",
             "state": "closed", "pull_request": None},
        ]) is None


class TestTheNextMaintenanceReleaseIsChecked:
    """`release/25.10.5` fell through every sieve, and it is the one that reaches users.

    Shipped versions come from `TS-*` TAGS; unreleased ones come from `release/*`
    BRANCHES that carry `-BETA`/`-RC`. A branched-but-untagged MAINTENANCE release is
    neither: no tag, and its line (25.10) has already shipped, so the "prereleases of
    a shipped line are history" filter threw it out. It was invisible.

    That is backwards. `release/24.10-RC.2` is history -- nobody can install it. But
    `release/25.10.5` is the FUTURE of a shipped line: it is what a 25.10.4 box gets
    on its next update. A break there ships to real users before the daily check has
    ever looked at it.
    """

    TAGS = ["TS-24.10.2.4", "TS-25.04.2.6", "TS-25.10.4"]
    HEADS = [
        "release/25.10.4.1",
        "release/25.10.5",          # branched, untagged -- the next maintenance release
        "release/24.10-RC.2",       # history: its line shipped long ago
        "release/25.20.2.2",        # iX's typo branch: 25.20 is not a TrueNAS version
        "release/26.0.0-BETA.3",
        "master",
    ]

    def _refs(self, monkeypatch):
        monkeypatch.setattr(
            compat, "_ls_remote",
            lambda remote, what: self.TAGS if what == "--tags" else self.HEADS)
        return compat.discover_refs("origin")

    def test_the_next_maintenance_release_is_checked(self, monkeypatch):
        assert "release/25.10.5" in self._refs(monkeypatch), (
            "the next thing a 25.10.4 box updates to is not checked, so a break in it "
            "reaches users before the bot ever sees it"
        )

    def test_a_superseded_maintenance_branch_is_not(self, monkeypatch):
        # 25.10.4.1 sorts OLDER than the newest tag TS-25.10.4? No -- it is NEWER, and
        # both are on the 25.10 line, so only the newest branch on the line is taken.
        refs = self._refs(monkeypatch)
        assert "release/25.10.4.1" not in refs, "only the newest branch per line"

    def test_the_typo_branch_stays_out(self, monkeypatch):
        # 25.20 has no TS tag, so it is not a release line at all. A typo branch in the
        # matrix reads as a real supported release we are silently broken on.
        assert "release/25.20.2.2" not in self._refs(monkeypatch)

    def test_a_prerelease_of_an_already_shipped_line_stays_out(self, monkeypatch):
        assert "release/24.10-RC.2" not in self._refs(monkeypatch)

    def test_an_untagged_branch_counts_as_UNRELEASED(self, monkeypatch):
        # The exit code keys off this. Calling 25.10.5 "shipped" would fail the build
        # as a live outage on a version nobody is running yet.
        assert compat.is_unreleased("release/25.10.5")
        assert compat.is_unreleased("master")
        assert not compat.is_unreleased("TS-25.10.4")


class TestMasterIsNotTheNextRelease:
    """A red `master` row used to read as "the version you are about to install".

    On 2026-07-14 master was 27-dev -- every recent commit targeted 27.0.0-BETA.1 --
    while 26 was still in beta on its own branches. So `master BROKEN` meant "iX will
    break us a major release from now", but the matrix said "master _(unreleased)_",
    which any reader takes as the next thing out the door. For a table whose whole job
    is helping somebody decide whether to trust this with their backups, that is a
    false alarm in the worst possible place.
    """

    def _rows(self, refs):
        return [{"ref": r, "unreleased": compat.is_unreleased(r), "modules": {}}
                for r in refs]

    def test_master_is_labelled_with_the_major_AFTER_the_newest_known_one(self):
        rows = self._rows(["TS-25.10.4", "release/26.0.0-BETA.3", "master"])
        assert compat.dev_label(rows) == "27-dev"

    def test_it_rolls_over_on_its_own_when_the_next_beta_branches(self):
        # Derived, not hardcoded: when release/27.0.0-BETA.1 appears, master is 28-dev.
        rows = self._rows(["TS-26.0.0", "release/27.0.0-BETA.1", "master"])
        assert compat.dev_label(rows) == "28-dev"

    def test_the_rendered_matrix_says_dev_not_unreleased(self):
        healthy = check_files(with_())
        rows = [
            {"ref": r, "unreleased": compat.is_unreleased(r), "modules": healthy}
            for r in ("TS-25.10.4", "release/26.0.0-BETA.3", "master")
        ]
        md = compat.render_markdown(rows)
        assert "master _(27-dev)_" in md
        assert "master _(unreleased)_" not in md
        # ...and the ordinary rows are untouched.
        assert "| 25.10.4 |" in md
        assert "| 26.0.0-BETA.3 _(unreleased)_ |" in md
