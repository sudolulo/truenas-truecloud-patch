#!/usr/bin/env python3
"""
Patches the TrueNAS webui Angular bundle to show S3 and B2 credentials
in the TrueCloud Backup task form, instead of Storj only.

Angular's Ivy compiler inlines TypeScript string enum values as literals in
the compiled bundle, so the template binding:

    [filterByProviders]="[CloudSyncProviderName.Storj]"

appears verbatim in the minified JS as:

    "filterByProviders",["STORJ_IX"]

We replace that array to include S3 and B2. The file is backed up before
modification so uninstall.sh can restore it.

Safe to run multiple times — a marker string detects an already-patched file.
Exits 0 in all cases (warnings are printed to stdout and logged by apply.sh).
"""

import os
import re
import shutil

WEBUI_CANDIDATES = [
    "/usr/share/truenas/webui",
    "/usr/share/truenas-ui",
    "/var/www/truenas",
]

# Angular's Ivy template compiler serialises the Storj-only filter as this
# exact substring in every production build we've observed.
FIND = re.compile(r'("filterByProviders",)\["STORJ_IX"\]')
REPLACE = r'\1["STORJ_IX","S3","B2"]'

# A patched file contains both "S3" and "B2" next to "STORJ_IX" in this form.
# This string is specific enough not to appear elsewhere in the bundle.
MARKER = '"STORJ_IX","S3","B2"'


def find_bundle():
    """
    Search WEBUI_CANDIDATES for the JS bundle containing the filterByProviders
    binding. Returns (webui_dir, path, content). webui_dir is None if no
    candidate directory exists; path is None if the directory exists but the
    pattern is not found in any bundle.
    """
    webui = next((d for d in WEBUI_CANDIDATES if os.path.isdir(d)), None)
    if webui is None:
        return None, None, None

    for root, _dirs, names in os.walk(webui):
        for name in sorted(names):          # deterministic order
            if not name.endswith(".js"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                if FIND.search(content) or MARKER in content:
                    return webui, path, content
            except OSError:
                continue

    return webui, None, None


def main():
    webui, path, content = find_bundle()
    if webui is None:
        print(
            "[truecloud-patch] WARNING: webui directory not found; skipping UI patch.\n"
            "[truecloud-patch] Searched: " + ", ".join(WEBUI_CANDIDATES)
        )
        return

    if path is None:
        print(
            "[truecloud-patch] WARNING: filterByProviders pattern not found in any JS bundle.\n"
            "[truecloud-patch] The TrueNAS webui may have been restructured in this version.\n"
            "[truecloud-patch] File an issue at https://github.com/sudolulo/truenas-truecloud-patch\n"
            f"[truecloud-patch] TrueNAS version info: {_tnversion()}"
        )
        return

    if MARKER in content:
        print(f"[truecloud-patch] UI already patched: {path}")
        return

    backup = path + ".pre-truecloud-patch"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)

    patched, count = FIND.subn(REPLACE, content)
    if count != 1:
        print(
            f"[truecloud-patch] WARNING: {count} replacement(s) in {path}; "
            f"expected exactly 1 — skipping write to avoid corrupting the bundle.\n"
            f"[truecloud-patch] File an issue at "
            f"https://github.com/sudolulo/truenas-truecloud-patch"
        )
        return

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(patched)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[truecloud-patch] ERROR: Could not write {path}: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return

    print(f"[truecloud-patch] UI bundle patched ({count} replacement(s)): {path}")


def _tnversion():
    try:
        with open("/etc/version") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


if __name__ == "__main__":
    main()
