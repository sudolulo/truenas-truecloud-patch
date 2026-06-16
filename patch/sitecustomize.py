"""
TrueCloud provider patch — sitecustomize.py
Installed into Python site-packages on every boot by apply.sh.

Hooks two middlewared module imports using the find_spec / exec_module API
(required for Python 3.12+, which ships with TrueNAS SCALE 25.x / Debian 13):

  middlewared.rclone.remote.b2
      Adds get_restic_config() so the native restic B2 backend works.
      Restic repo URL: b2:<bucket>:<folder>   (colon separator, restic 0.16.x)
      Auth: B2_ACCOUNT_ID, B2_ACCOUNT_KEY

  middlewared.plugins.cloud_backup.restic
      Fixes the URL builder for providers with no hostname component, and
      converts the slash separator to a colon for B2 (restic 0.16.x format):
      Stock code: f"{rclone_type}:{url}/{remote_path}" → "b2:/bucket/path"
      Patched:    "b2:bucket:path" (leading slash stripped, / → : for B2)

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
                # Module absent in this Python installation (e.g. removed in a
                # TrueNAS update).  Record a FAIL so hook_status.json is still
                # written and cmd_verify gives a diagnostic instead of "no file".
                _record_status(fullname, ok=False,
                               detail="module not found in this Python installation")
                self._mark_done(fullname)
                return None

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
        except Exception as exc:
            # Mark done to prevent infinite retry loops, then surface the failure
            # in hook_status.json so cmd_verify shows a diagnostic FAIL rather
            # than "no status file found".
            self._finder._mark_done(fullname)
            _record_status(fullname, ok=False,
                           detail=f"module load failed: {exc}")
            raise
        self._finder._mark_done(fullname)

        # exec_module succeeded — apply our patch.
        # Must stay in sync with _Finder._targets.
        try:
            if fullname == "middlewared.rclone.remote.b2":
                _patch_b2(module)
            elif fullname == "middlewared.plugins.cloud_backup.restic":
                _patch_restic(module)
        except Exception as exc:
            sys.stderr.write(
                f"[truecloud-patch] patch failed for {fullname}: {exc}\n"
            )
            _record_status(fullname, ok=False, detail=str(exc))


# ── Patch functions ───────────────────────────────────────────────────────────

def _patch_b2(module):
    cls = module.B2RcloneRemote

    if "get_restic_config" in cls.__dict__:
        # A future TrueNAS version already added native B2 restic support.
        _record_status("middlewared.rclone.remote.b2", ok=True,
                       detail="native support present; patch not needed")
        return

    def _b2_restic_config(task):
        p = task["credentials"]["provider"]
        missing = [f for f in ("account", "key") if f not in p]
        if missing:
            raise KeyError(
                f"truecloud-patch: B2 provider missing field(s) {missing!r}; "
                f"schema may have changed. Present: {sorted(p)!r}"
            )
        return "", {"B2_ACCOUNT_ID": p["account"], "B2_ACCOUNT_KEY": p["key"]}

    cls.get_restic_config = staticmethod(_b2_restic_config)
    cls.restic = True
    sys.stderr.write("[truecloud-patch] B2 restic support enabled\n")
    _record_status("middlewared.rclone.remote.b2", ok=True,
                   detail="method attached; credential fields verified at first backup")


def _patch_restic(module):
    if getattr(module.get_restic_config, "_truecloud_patched", False):
        _record_status("middlewared.plugins.cloud_backup.restic", ok=True,
                       detail="already patched in this process")
        return

    import dataclasses

    _orig = module.get_restic_config

    def get_restic_config(cloud_backup):
        # Call the original — it handles cache, RESTIC_PASSWORD, env, etc.
        result = _orig(cloud_backup)

        # Fix the repo URL in the restic command.
        # Stock middlewared builds: b2:/bucket/path
        # restic 0.16.x B2 expects: b2:bucket:path  (colon separator, no leading slash)
        # "scheme://path" (Storj) must not be touched.
        cmd = list(result.cmd)
        for i, part in enumerate(cmd):
            if part.startswith("--repo=") or part.startswith("--repository="):
                pfx, _, url = part.partition("=")
                pfx += "="
            elif i and cmd[i - 1] in ("-r", "--repo", "--repository"):
                pfx = None
                url = part
            else:
                continue
            scheme, sep, rest = url.partition(":")
            if not sep:
                break
            changed = False
            if rest.startswith("/") and not rest.startswith("//"):
                rest = rest[1:]
                changed = True
            if scheme == "b2" and "/" in rest:
                rest = rest.replace("/", ":", 1)
                changed = True
            if changed:
                new_url = scheme + ":" + rest
                cmd[i] = pfx + new_url if pfx is not None else new_url
                try:
                    return dataclasses.replace(result, cmd=cmd)
                except TypeError:
                    return result._replace(cmd=cmd)
            break
        return result

    get_restic_config._truecloud_patched = True
    module.get_restic_config = get_restic_config
    sys.stderr.write("[truecloud-patch] restic B2 URL fix applied (b2:bucket:path)\n")
    _record_status("middlewared.plugins.cloud_backup.restic", ok=True)


_STATUS_FILE = "/data/truecloud-patch/hook_status.json"
_hook_status: dict = {}


def _record_status(fullname: str, ok: bool, detail: str = "") -> None:
    import json
    import os
    import time

    if fullname in _hook_status:
        return  # idempotent: first call wins
    _hook_status[fullname] = {"ok": ok, "detail": detail}

    payload = {
        "patched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "patches": _hook_status,
    }
    try:
        tmp = _STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, _STATUS_FILE)  # atomic on POSIX
    except OSError:
        pass  # non-fatal — status file is informational only


# ── Entry point ───────────────────────────────────────────────────────────────

def _install():
    import os
    if os.path.exists("/data/truecloud-patch/disabled"):
        return  # kill switch
    # Scope to the middlewared service process only — not midclt, debug scripts,
    # or other tools that happen to share the same venv.
    # Also covers python -m middlewared where argv[0] is the __main__.py path.
    _argv0 = (sys.argv or [""])[0]
    _base = os.path.basename(_argv0)
    if not (
        _base == "middlewared"
        or (_base == "__main__.py"
            and os.path.basename(os.path.dirname(_argv0)) == "middlewared")
    ):
        return
    import importlib.util
    if importlib.util.find_spec("middlewared") is None:
        return
    # If apply.sh displaced an existing sitecustomize.py, exec it first so any
    # vendor startup code (path additions, codec registrations, etc.) still runs.
    # __file__ is absent in some embedded contexts; the empty fallback is safe.
    _self = globals().get("__file__", "")
    if _self:
        _pre = _self + ".pre-truecloud-patch"
        if os.path.isfile(_pre):
            try:
                import builtins
                with open(_pre, encoding="utf-8") as _fh:
                    # Separate globals dict prevents the exec'd code from
                    # shadowing our names; sys.path changes still take effect
                    # via the shared sys module object.
                    exec(  # noqa: S102
                        compile(_fh.read(), _pre, "exec"),
                        {"__builtins__": builtins, "__file__": _pre,
                         "__name__": "sitecustomize"},
                    )
            except Exception:
                pass
    sys.meta_path.append(_Finder())


try:
    _install()
except Exception:
    pass  # never raise from sitecustomize.py — it would prevent Python from starting
