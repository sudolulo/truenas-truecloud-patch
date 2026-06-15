"""
TrueCloud provider patch — sitecustomize.py
Installed into Python site-packages on every boot by apply.sh.

Hooks the import of two middlewared modules and patches them in-place:

  middlewared.rclone.remote.b2
      Adds get_restic_config() so the native B2 restic backend works.
      Restic URL: b2:<bucket>/<folder>
      Auth env:   B2_ACCOUNT_ID, B2_ACCOUNT_KEY

  middlewared.plugins.cloud_backup.restic
      Fixes URL construction for providers that have no hostname component
      (url == ""). Stock code builds "b2:/bucket/path" (broken double-slash);
      patched code builds "b2:bucket/path".

Safe for all Python processes on the system: if middlewared is absent the
hook installs but never fires, and all errors are caught and logged to stderr.
"""

import sys


def _install():
    _pending = {
        "middlewared.rclone.remote.b2",
        "middlewared.plugins.cloud_backup.restic",
    }
    _loading = set()

    class _Hook:
        def find_module(self, fullname, path=None):  # noqa: ARG002
            if fullname in _pending and fullname not in _loading:
                return self
            return None

        def load_module(self, fullname):
            if fullname in sys.modules:
                module = sys.modules[fullname]
            else:
                _loading.add(fullname)
                try:
                    __import__(fullname)
                finally:
                    _loading.discard(fullname)
                module = sys.modules[fullname]

            _pending.discard(fullname)

            try:
                if fullname == "middlewared.rclone.remote.b2":
                    _patch_b2(module)
                elif fullname == "middlewared.plugins.cloud_backup.restic":
                    _patch_restic(module)
            except Exception as exc:
                sys.stderr.write(f"[truecloud-patch] patch failed for {fullname}: {exc}\n")

            if not _pending:
                try:
                    sys.meta_path.remove(hook)
                except ValueError:
                    pass

            return module

    hook = _Hook()
    sys.meta_path.append(hook)


def _patch_b2(module):
    cls = module.B2RcloneRemote

    if hasattr(cls, "get_restic_config"):
        return  # future TrueNAS version already added it

    def get_restic_config(self, task):
        p = task["credentials"]["provider"]
        return "", {
            "B2_ACCOUNT_ID": p["account"],
            "B2_ACCOUNT_KEY": p["key"],
        }

    cls.get_restic_config = get_restic_config
    cls.restic = True
    sys.stderr.write("[truecloud-patch] B2 restic support enabled\n")


def _patch_restic(module):
    orig = module.get_restic_config
    if getattr(orig, "_truecloud_patched", False):
        return

    # Capture module-level references; REMOTES is the same mutable dict
    # object that remotes.setup() will populate later.
    _REMOTES = module.REMOTES
    _get_remote_path = module.get_remote_path
    _ResticConfig = module.ResticConfig

    def get_restic_config(cloud_backup):
        remote = _REMOTES[cloud_backup["credentials"]["provider"]["type"]]
        remote_path = _get_remote_path(remote, cloud_backup["attributes"])
        url, env = remote.get_restic_config(cloud_backup)

        if cloud_backup["cache_path"]:
            cache = ["--cache-dir", cloud_backup["cache_path"]]
        else:
            cache = ["--no-cache"]

        # Fix: stock code does f"{rclone_type}:{url}/{remote_path}" which
        # produces "b2:/bucket/path" when url is empty.
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


try:
    import importlib.util
    if importlib.util.find_spec("middlewared") is not None:
        _install()
except Exception:
    pass
