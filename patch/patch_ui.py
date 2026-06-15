#!/usr/bin/env python3
"""
Patches the TrueNAS webui Angular bundle to show S3 and B2 credentials
in the TrueCloud Backup task form, instead of Storj only.

The compiled bundle contains:
    "filterByProviders",["STORJ_IX"]
which is the template binding [filterByProviders]="[CloudSyncProviderName.Storj]".
We replace the array with ["STORJ_IX","S3","B2"] so all three providers appear.

Run automatically by apply.sh on every boot. Safe to run multiple times.
"""

import os
import re
import shutil
import sys

WEBUI_CANDIDATES = [
    "/usr/share/truenas/webui",
    "/usr/share/truenas-ui",
    "/var/www/truenas",
]

# The pattern as compiled by Angular's Ivy into minified JS.
# String literals survive minification; the function name before the comma
# is mangled and not part of our match.
FIND = re.compile(r'("filterByProviders",)\["STORJ_IX"\]')
REPLACE = r'\1["STORJ_IX","S3","B2"]'

# Presence of this string means we already patched this file.
MARKER = '"STORJ_IX","S3","B2"'


def find_webui():
    for d in WEBUI_CANDIDATES:
        if os.path.isdir(d):
            return d
    return None


def find_bundle(webui):
    for root, _, names in os.walk(webui):
        for name in names:
            if not name.endswith(".js"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path) as fh:
                    content = fh.read()
                if FIND.search(content):
                    return path, content
            except (UnicodeDecodeError, PermissionError, OSError):
                continue
    return None, None


def main():
    webui = find_webui()
    if not webui:
        print("[truecloud-patch] WARNING: webui directory not found, skipping UI patch")
        sys.exit(0)

    path, content = find_bundle(webui)
    if path is None:
        print(
            "[truecloud-patch] WARNING: filterByProviders pattern not found in webui bundle.\n"
            "[truecloud-patch] The UI patch may need updating for this TrueNAS version.\n"
            "[truecloud-patch] File an issue at https://github.com/sudolulo/truenas-truecloud-patch"
        )
        sys.exit(0)

    if MARKER in content:
        print(f"[truecloud-patch] UI already patched: {path}")
        sys.exit(0)

    backup = path + ".pre-truecloud-patch"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)

    patched, count = FIND.subn(REPLACE, content)
    with open(path, "w") as fh:
        fh.write(patched)

    print(f"[truecloud-patch] UI bundle patched ({count} replacement(s)): {path}")


if __name__ == "__main__":
    main()
