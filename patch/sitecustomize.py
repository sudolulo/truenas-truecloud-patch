"""
TrueCloud provider patch — sitecustomize.py
Installed into Python site-packages on every boot by apply.sh.

Hooks two middlewared module imports using the find_spec / exec_module API
(required for Python 3.12+, which ships with TrueNAS SCALE 25.x / Debian 13):

  middlewared.rclone.remote.b2
      Adds get_restic_config() so the native restic B2 backend works.
      Restic repo URL: b2:<bucket>/<folder>
      Auth: B2_ACCOUNT_ID, B2_ACCOUNT_KEY

  middlewared.plugins.cloud_backup.restic
      Fixes the URL builder for providers with no hostname component.
      Stock code: f"{rclone_type}:{url}/{remote_path}" → "b2:/bucket/path" (broken)
      Patched:    "b2:bucket/path" when url == ""

Both patches are no-ops if the module already provides the functionality
(i.e. a future TrueNAS version adds native support). All errors are caught
and written to stderr so middlewared always starts regardless of patch state.
"""

import sys


# ── Import hook ───────────────────────────────────────────────────────────────

class _Finder:
    """
    find_spec-based meta path finder (Python 3.4+, required for 3.12+).
    Intercepts specific module imports, loads them normally, then patches.
    """

    _targets = frozenset({
        "middlewared.rclone.remote.b2",
        "middlewared.plugins.cloud_backup.restic",
    })

    def __init__(self):
        self._loading = set()   # guards against re-entrant imports
        self._done = set()      # modules already patched

    def find_spec(self, fullname, path, target=None):  # noqa: ARG002
        if (
            fullname in self._targets
            and fullname not in self._done
            and fullname not in self._loading
        ):
            import importlib.machinery
            import importlib.util

            # Find the real file spec HERE, before Python adds the module to
            # sys.modules.  If we deferred this to exec_module, find_spec would
            # short-circuit via sys.modules[fullname].__spec__ (our own spec) and
            # exec_module would call itself recursively forever.
            self._loading.add(fullname)
            try:
                real_spec = importlib.util.find_spec(fullname)
            finally:
                self._loading.discard(fullname)

            if real_spec is None:
                return None  # module doesn't exist; don't intercept

            return importlib.machinery.ModuleSpec(
                fullname,
                _Loader(self, fullname, real_spec),
                origin=real_spec.origin,
                is_package=real_spec.submodule_search_locations is not None,
            )
        return None

    def _mark_done(self, fullname):
        self._done.add(fullname)
        if self._done >= self._targets:
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass


class _Loader:
    def __init__(self, finder, fullname, real_spec):
        self._finder = finder
        self._fullname = fullname
        self._real_spec = real_spec

    def create_module(self, spec):  # noqa: ARG002
        return None  # use Python's default module creation

    def exec_module(self, module):
        fullname = self._fullname
        real_spec = self._real_spec

        try:
            real_spec.loader.exec_module(module)
            # Fix module metadata so it looks like a normal import.
            module.__spec__ = real_spec
            module.__loader__ = real_spec.loader
            if real_spec.origin:
                module.__file__ = real_spec.origin
        finally:
            # Mark done even on failure so a broken module doesn't cause
            # infinite retry loops on subsequent import attempts.
            self._finder._mark_done(fullname)

        # exec_module succeeded — apply our patch.
        try:
            _PATCHES[fullname](module)
        except Exception as exc:
            sys.stderr.write(
                f"[truecloud-patch] patch failed for {fullname}: {exc}\n"
            )


# ── Patch functions ───────────────────────────────────────────────────────────

def _patch_b2(module):
    cls = module.B2RcloneRemote

    if hasattr(cls, "get_restic_config"):
        # A future TrueNAS version already added native B2 restic support.
        return

    def get_restic_config(self, task):  # noqa: ARG001
        p = task["credentials"]["provider"]
        return "", {
            "B2_ACCOUNT_ID": p["account"],
            "B2_ACCOUNT_KEY": p["key"],
        }

    cls.get_restic_config = get_restic_config
    cls.restic = True
    sys.stderr.write("[truecloud-patch] B2 restic support enabled\n")


def _patch_restic(module):
    if getattr(module.get_restic_config, "_truecloud_patched", False):
        return

    # ResticConfig is safe to capture now (it's a dataclass defined in the module).
    # REMOTES and get_remote_path are imported lazily inside the function so that
    # module layout changes in future middlewared versions fail at call time
    # (during an actual backup job) rather than silently at patch time.
    _ResticConfig = module.ResticConfig

    def get_restic_config(cloud_backup):
        from middlewared.plugins.cloud.path import get_remote_path
        from middlewared.plugins.cloud.remotes import REMOTES

        remote = REMOTES[cloud_backup["credentials"]["provider"]["type"]]
        remote_path = get_remote_path(remote, cloud_backup["attributes"])
        url, env = remote.get_restic_config(cloud_backup)

        if cloud_backup["cache_path"]:
            cache = ["--cache-dir", cloud_backup["cache_path"]]
        else:
            cache = ["--no-cache"]

        # Stock code produces "b2:/bucket/path" when url == "" (double-slash).
        repo = (
            f"{remote.rclone_type}:{url}/{remote_path}"
            if url
            else f"{remote.rclone_type}:{remote_path}"
        )
        cmd = ["restic"] + cache + ["--json", "-r", repo]
        env["RESTIC_PASSWORD"] = cloud_backup["password"]
        return _ResticConfig(cmd, env)

    get_restic_config._truecloud_patched = True
    module.get_restic_config = get_restic_config
    sys.stderr.write("[truecloud-patch] restic URL fix applied\n")


_PATCHES = {
    "middlewared.rclone.remote.b2": _patch_b2,
    "middlewared.plugins.cloud_backup.restic": _patch_restic,
}


# ── Entry point ───────────────────────────────────────────────────────────────

def _install():
    import os
    if os.path.exists("/data/truecloud-patch/disabled"):
        return  # kill switch: touch /data/truecloud-patch/disabled to bypass this hook
    import importlib.util
    if importlib.util.find_spec("middlewared") is None:
        return  # not a middlewared Python process; nothing to do
    sys.meta_path.append(_Finder())


try:
    _install()
except Exception:
    pass  # never raise from sitecustomize.py — it would prevent Python from starting
