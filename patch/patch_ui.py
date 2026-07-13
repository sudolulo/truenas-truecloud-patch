#!/usr/bin/env python3
"""
Patches the TrueNAS webui Angular bundle to show S3 and B2 credentials
in the TrueCloud Backup task form, instead of Storj only.

The template binding [filterByProviders]="[CloudSyncProviderName.Storj]" appears
in the minified JS in one of two forms depending on TrueNAS / Angular version:

  TrueNAS 24.x (static inline array):
      "filterByProviders",["STORJ_IX"]

  TrueNAS 25.x+ (Angular pureFunction binding):
      "filterByProviders",pe(115,Rn,i.CloudSyncProviderName.Storj)

Both are replaced so the dropdown includes S3 and B2. The file is backed up
before modification so uninstall.sh can restore it.

Safe to run multiple times — a marker string detects an already-patched file.
Exits 0 in all cases (warnings are printed to stdout and logged by apply.sh).
"""

import contextlib
import os
import re
import shutil

WEBUI_CANDIDATES = [
    "/usr/share/truenas/webui",
    "/usr/share/truenas-ui",
    "/var/www/truenas",
]

# Patterns tried in order; the first match wins.
# Each entry is (compiled_regex, replacement_string).
_PATTERNS = [
    # TrueNAS 25.x+: Angular emits a pureFunction call instead of a literal array.
    (re.compile(r'("filterByProviders",)\w+\(\d+,\w+,\w+\.CloudSyncProviderName\.Storj\)\)'),
     r'\1["STORJ_IX","S3","B2"])'),


    # TrueNAS 24.x and earlier: static inline array.
    (re.compile(r'("filterByProviders",)\["STORJ_IX"\]'),
     r'\1["STORJ_IX","S3","B2"]'),
]

# Present in any patched file. Specific enough not to appear elsewhere.
MARKER = '"STORJ_IX","S3","B2"'


def _match_pattern(content):
    """Return (regex, replacement) for the first pattern found in content, or (None, None)."""
    for find, replace in _PATTERNS:
        if find.search(content):
            return find, replace
    return None, None


def find_bundle():
    """
    Search WEBUI_CANDIDATES for the JS chunk containing the filterByProviders
    binding. Returns (webui_dir, path, content). webui_dir is None if no
    candidate directory exists; path is None if the directory exists but the
    pattern is not found in any file.
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
                find, _ = _match_pattern(content)
                if find is not None or MARKER in content:
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

    find, replace = _match_pattern(content)

    backup = path + ".pre-truecloud-patch"
    if not os.path.exists(backup):
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            print(f"[truecloud-patch] ERROR: Could not create backup {backup}: {exc}")
            return

    patched, count = find.subn(replace, content)
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
        with contextlib.suppress(OSError):
            os.unlink(tmp)
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
