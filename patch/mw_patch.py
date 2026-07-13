#!/usr/bin/env python3
"""Apply and revert truecloud-patch's blocks in middlewared's modules.

Every patch this project makes to a middlewared module is an appended block that
begins with the MARKER line. That makes patching idempotent (truncate at the
marker, re-append) and reverting exact (truncate at the marker, stop).

This is the single implementation of that. It used to live in two places --
apply.sh's heredoc and an inline heredoc in uninstall.sh -- and the uninstall copy
was the untested one.

    python3 mw_patch.py revert-all       # remove every block + the nested module
    python3 mw_patch.py revert-nested    # remove only the nested module's blocks
"""

from __future__ import annotations

import os
import sys

MARKER = "\n# TRUECLOUD_PATCH"

#: Modules the providers module (B2/S3) patches.
PROVIDER_RELPATHS = [
    ("rclone", "remote", "b2.py"),
    ("plugins", "cloud_backup", "restic.py"),
]

#: Modules the nested-snapshot module patches. Order matters on revert -- see
#: revert(): the loadable module goes first.
NESTED_RELPATHS = [
    ("plugins", "cloud", "crud.py"),
    ("plugins", "cloud_backup", "sync.py"),
    ("plugins", "cloud", "snapshot.py"),
]

#: The importable module the nested blocks depend on.
NESTED_MODULE = ("plugins", "cloud", "_truecloud_nested.py")


def patch_file(path, block):
    """Append `block`, replacing any block we appended before. Idempotent."""
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    idx = content.find(MARKER)
    base = content[:idx] if idx != -1 else content
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(base.rstrip("\n") + "\n" + block)


def unpatch_file(path):
    """Strip our appended block, restoring the stock file. True if it was patched."""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return False
    idx = content.find(MARKER)
    if idx == -1:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content[:idx].rstrip("\n") + "\n")
    except OSError:
        return False
    return True


def revert(mw_dir, relpaths, module_relpath=None):
    """Remove our blocks from `relpaths`, and the module at `module_relpath`.

    The module is deleted FIRST. Every injected block is guarded by
    `if _tc_nested is not None`, so once the module is gone the blocks all no-op
    even if a later unpatch fails -- the stock guard comes back regardless.

    Returns the names of what was actually reverted.
    """
    reverted = []

    if module_relpath:
        try:
            os.unlink(os.path.join(mw_dir, *module_relpath))
            reverted.append(module_relpath[-1])
        except OSError:
            pass

    for rel in relpaths:
        if unpatch_file(os.path.join(mw_dir, *rel)):
            reverted.append(rel[-1])

    return reverted


def revert_nested(mw_dir):
    """Undo the nested-snapshot patch only. Leaves the providers patch alone.

    restic.py also carries a block, but it belongs to the providers module --
    reverting it would silently break B2 backups.
    """
    return revert(mw_dir, NESTED_RELPATHS, NESTED_MODULE)


def revert_all(mw_dir):
    """Undo every patch this project applies."""
    return revert(mw_dir, NESTED_RELPATHS + PROVIDER_RELPATHS, NESTED_MODULE)


def find_middlewared_dir():
    """Directory of the installed `middlewared` package, or None."""
    try:
        import middlewared
    except ImportError:
        return None
    return os.path.dirname(os.path.abspath(middlewared.__file__))


def main(argv):
    if len(argv) < 2 or argv[1] not in ("revert-all", "revert-nested"):
        print(__doc__, file=sys.stderr)
        return 2

    mw_dir = find_middlewared_dir()
    if mw_dir is None:
        print("  middlewared not importable — nothing to revert.")
        return 0

    fn = revert_all if argv[1] == "revert-all" else revert_nested
    reverted = fn(mw_dir)
    if reverted:
        print("  Reverted: " + ", ".join(reverted))
    else:
        print("  Nothing to revert (overlay already removed, or never patched).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
