#!/usr/bin/env python3
"""What this patch assumes about middlewared -- written down, and checkable.

WHY THIS EXISTS
---------------
This patch appends code to middlewared's own modules. middlewared has no stability
contract: it is internal API, and iX may reshape it in any release. When they do,
the patch does not politely decline -- it breaks a backup, possibly silently, which
is the worst thing a backup tool can do.

It has already happened. TrueNAS 26 rewrites the whole cloud_backup path from
async to synchronous:

    25.10:  async def create_snapshot(...)   /  await create_snapshot(...)
    26.0:         def create_snapshot(...)   /        create_snapshot(...)

Every block the nested module injects is an `async def` wrapping an `await`ed
original. On 26 that unpacks a coroutine object instead of a tuple. Nobody would
have found out until a restore failed.

So the assumptions are written down here, once, and checked in two places:

  * .github/workflows/compat.yml runs `--ref` against TrueNAS's *unreleased*
    branches (master, the newest BETA/RC) on a schedule, and opens a bug report
    the day iX breaks us -- while it is still a beta, not after it ships.

  * patch/apply.sh runs `--tree` against the middlewared *actually installed*, at
    every boot, and REFUSES to patch a module whose assumptions no longer hold.
    That is the guarantee: an unpatched module means stock TrueNAS (Storj only,
    but working). A patched-anyway module means broken backups. Declining is
    always the better failure.

The two modules are checked independently, because they fail independently: on 26
the providers module (B2/S3) only touches synchronous symbols and survives, while
the nested module does not.

    python3 tools/compat.py --tree /usr/lib/python3/dist-packages/middlewared
    python3 tools/compat.py --ref release/26.0.0-BETA.3
    python3 tools/compat.py --ref master --json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import urllib.request

PROVIDERS = "providers"
NESTED = "nested"

RAW = "https://raw.githubusercontent.com/truenas/middleware/{ref}/src/middlewared/middlewared/{path}"

_TIMEOUT = 30


class Assumption:
    """One thing that must be true of middlewared, or a module cannot be applied.

    `is_async=None` means "do not care". Everywhere else it is stated explicitly,
    because asyncness is exactly the axis TrueNAS 26 changed and a checker that
    ignored it would have passed a build that breaks every backup.
    """

    def __init__(self, ident, module, path, symbol, *, kind="function",
                 is_async=None, params=None, why=""):
        self.id = ident
        self.module = module
        self.path = path
        self.symbol = symbol
        self.kind = kind
        self.is_async = is_async
        self.params = params or []
        self.why = why


#: Everything patch/apply.sh's injected blocks depend on. Derived from the blocks
#: themselves -- if you add a block, add its assumptions here or the checker is
#: decoration.
ASSUMPTIONS = [
    # ── providers (B2/S3). Touches only synchronous symbols. ──────────────────
    Assumption(
        "b2-remote-class", PROVIDERS, "rclone/remote/b2.py", "B2RcloneRemote",
        kind="class",
        why="B2_BLOCK sets .get_restic_config and .restic on this class",
    ),
    Assumption(
        "restic-config-fn", PROVIDERS, "plugins/cloud_backup/restic.py",
        "get_restic_config", is_async=False, params=["cloud_backup"],
        why="RESTIC_BLOCK wraps it to rewrite the repo URL; it calls the original "
            "WITHOUT await, so it must stay synchronous",
    ),
    Assumption(
        "restic-config-class", PROVIDERS, "plugins/cloud_backup/restic.py",
        "ResticConfig", kind="class",
        why="RESTIC_BLOCK does dataclasses.replace(result, cmd=...) on what "
            "get_restic_config returns",
    ),

    # ── nested snapshots. Every block here is an async wrapper. ───────────────
    Assumption(
        "create-snapshot", NESTED, "plugins/cloud/snapshot.py", "create_snapshot",
        is_async=True, params=["middleware", "path", "name"],
        why="SNAPSHOT_BLOCK replaces it with `async def` that AWAITS the original "
            "and returns (snapshot, staging_root). TrueNAS 26 made it synchronous: "
            "the wrapper would return a coroutine that sync.py unpacks as a tuple",
    ),
    Assumption(
        "crud-mixin-validate", NESTED, "plugins/cloud/crud.py",
        "CloudTaskServiceMixin._validate",
        kind="method", is_async=True, params=["self", "app", "verrors", "name", "data"],
        why="CRUD_BLOCK replaces it with `async def` that AWAITS the original, to "
            "drop the no-further-nesting error",
    ),
    Assumption(
        "restic-backup", NESTED, "plugins/cloud_backup/sync.py", "restic_backup",
        is_async=True, params=["middleware", "job", "cloud_backup"],
        why="SYNC_BLOCK replaces it with `async def` that AWAITS the original, to "
            "tear down bind mounts in a finally",
    ),
]


#: Things that mean iX has done the job themselves and the module should RETIRE,
#: not break. Absence of the nesting guard = nested snapshots went native.
#: `restic = True` already on B2RcloneRemote = B2 restic support went native.
NATIVE_PROBES = {
    NESTED: (
        "plugins/cloud/crud.py",
        "no further nesting",
        False,  # native when the phrase is ABSENT
    ),
    PROVIDERS: (
        "rclone/remote/b2.py",
        "restic = True",
        True,   # native when the phrase is PRESENT
    ),
}


def _squash(text: str) -> str:
    """Drop whitespace and quotes, so a phrase split across string literals matches.

    Stock middleware writes the guard as an implicitly-concatenated literal:

        verrors.add(f"{name}.snapshot", "This option is only available for "
                                        "datasets that have no further nesting")

    A naive `"no further nesting" in source` is therefore FALSE on a version that
    very much has the guard -- and this probe's False means "TrueNAS supports it
    natively, retire the module". That is a silent, catastrophic misread: it would
    disable nested snapshots on every box that currently depends on them.

    apply.sh already learned this the hard way and normalises the same way. Both
    now call this one function, which is the only reason they cannot drift apart
    again.
    """
    return text.translate(str.maketrans("", "", " \t\n\r\"'"))


# ── AST lookups ──────────────────────────────────────────────────────────────

def _find(tree, symbol):
    """The node for `name` or `Class.method`, or None."""
    if "." in symbol:
        cls_name, meth = symbol.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) \
                            and sub.name == meth:
                        return sub
        return None

    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) \
                and node.name == symbol:
            return node
    return None


def _params(node):
    a = node.args
    return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def check_source(a: Assumption, src: str | None) -> str | None:
    """The reason assumption `a` no longer holds, or None if it does."""
    if src is None:
        return f"{a.path} does not exist"

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"{a.path} does not parse: {e}"

    node = _find(tree, a.symbol)
    if node is None:
        return f"{a.path} no longer defines {a.symbol}"

    if a.kind == "class":
        if not isinstance(node, ast.ClassDef):
            return f"{a.symbol} is no longer a class"
        return None

    if isinstance(node, ast.ClassDef):
        return f"{a.symbol} is a class, expected a function"

    got_async = isinstance(node, ast.AsyncFunctionDef)
    if a.is_async is not None and got_async != a.is_async:
        want = "async def" if a.is_async else "def"
        got = "async def" if got_async else "def"
        return (
            f"{a.symbol} is now `{got}`, the patch requires `{want}` "
            f"({a.path})"
        )

    have = _params(node)
    missing = [p for p in a.params if p not in have]
    if missing:
        return (
            f"{a.symbol}{tuple(have)} no longer takes {', '.join(missing)}"
        )

    return None


# ── sources ──────────────────────────────────────────────────────────────────

def _fetch(ref: str, path: str) -> str | None:
    url = RAW.format(ref=ref, path=path)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:  # noqa: S310
            if r.status != 200:
                return None
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _read(root: str, path: str) -> str | None:
    try:
        with open(os.path.join(root, *path.split("/")), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def check(loader, modules=None) -> dict:
    """Check every assumption. `loader(path) -> source|None`.

    Returns {module: {"ok": bool, "native": bool, "problems": [...]}}.
    """
    modules = modules or [PROVIDERS, NESTED]
    cache = {}

    def src(path):
        if path not in cache:
            cache[path] = loader(path)
        return cache[path]

    out = {m: {"ok": True, "native": False, "problems": []} for m in modules}

    for a in ASSUMPTIONS:
        if a.module not in out:
            continue
        problem = check_source(a, src(a.path))
        if problem:
            out[a.module]["ok"] = False
            out[a.module]["problems"].append({
                "id": a.id, "detail": problem, "why": a.why,
            })

    for module, (path, phrase, native_when_present) in NATIVE_PROBES.items():
        if module not in out:
            continue
        text = src(path)
        if text is None:
            continue
        present = _squash(phrase) in _squash(text)
        out[module]["native"] = (present == native_when_present)

    return out


def check_ref(ref: str, modules=None) -> dict:
    return check(lambda p: _fetch(ref, p), modules)


def check_tree(root: str, modules=None) -> dict:
    return check(lambda p: _read(root, p), modules)


# ── which TrueNAS versions to check ──────────────────────────────────────────

REPO = "https://github.com/truenas/middleware"

#: TrueCloud Backup -- the restic-based cloud_backup this patch extends -- was
#: introduced in 24.10. In 24.04 the modules simply do not exist (404), which the
#: checker would otherwise report as three separate "broken assumptions" for a
#: feature that was never there.
OLDEST = (24, 10)

#: BETA < RC < shipped. Without this, "26.0.0-BETA.1" and "26.0.0-BETA.3" both
#: reduce to (26,0,0) and the matrix silently reports whichever was seen first --
#: which is how it first showed BETA.1 while BETA.3 was the one to worry about.
_STAGE = {"BETA": 0, "RC": 1}
_SHIPPED = 2


def _version_of(name: str):
    """Sortable version of 'release/26.0.0-BETA.3' or 'TS-25.10.4'. None if junk.

    Returns ((major, minor, ...), stage_rank, stage_number).
    """
    tail = name.split("/", 1)[1] if "/" in name else name
    tail = tail.removeprefix("TS-")

    core, _, suffix = tail.partition("-")
    try:
        version = tuple(int(p) for p in core.split("."))
    except ValueError:
        return None
    if len(version) < 2:
        return None

    if not suffix:
        return version, _SHIPPED, 0

    stage, _, num = suffix.partition(".")
    rank = _STAGE.get(stage.upper())
    if rank is None:
        return None                      # not a release line we understand
    return version, rank, int(num) if num.isdigit() else 0


def _newest_per_line(names):
    """Newest name on each (major, minor) line."""
    best = {}
    for name in names:
        v = _version_of(name)
        if not v or v[0][:2] < OLDEST:
            continue
        key = v[0][:2]
        if key not in best or v > best[key][0]:
            best[key] = (v, name)
    return [n for _, n in sorted(best.values())]


def _ls_remote(remote, what):
    import subprocess

    out = subprocess.run(
        ["git", "ls-remote", what, "--refs", remote],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    prefix = "refs/tags/" if what == "--tags" else "refs/heads/"
    return [
        line.split(prefix, 1)[1].strip()
        for line in out.splitlines() if prefix in line
    ]


def discover_refs(remote: str = REPO) -> list[str]:
    """What to check: every shipped TrueNAS line, everything unreleased, and master.

    Two sources, because they are authoritative for different things:

      * SHIPPED comes from the `TS-*` TAGS. Those are what iX actually released.
        The `release/*` branches include mistakes -- `release/25.20.2.2` exists and
        25.20 is not a TrueNAS version -- and a typo branch in the matrix reads as
        a real supported release that we are silently broken on.

      * UNRELEASED comes from the BRANCHES, because that is where a beta appears
        first: `release/26.0.0-BETA.3` had no tag yet while it was the newest beta.
        Catching breakage here, before it ships, is the whole point of this file.
    """
    tags = _ls_remote(remote, "--tags")
    heads = _ls_remote(remote, "--heads")

    shipped = _newest_per_line([
        t for t in tags if t.startswith("TS-") and "-BETA" not in t and "-RC" not in t
    ])

    # A prerelease of a line that has ALREADY shipped is history, not a warning:
    # release/24.10-RC.2 still exists, and the nested module does not apply to it,
    # but 24.10 shipped long ago and TS-24.10.2.4 is fine. Reporting it would be a
    # standing red row in the matrix for a version nobody can install.
    shipped_lines = {_version_of(t)[0][:2] for t in shipped}
    upcoming = [
        h for h in _newest_per_line([
            h for h in heads
            if h.startswith("release/") and ("-BETA" in h or "-RC" in h)
        ])
        if _version_of(h)[0][:2] not in shipped_lines
    ]

    return [*shipped, *upcoming, "master"]


def is_unreleased(ref: str) -> bool:
    """master and any BETA/RC. Breakage here is early warning, not an outage."""
    return ref == "master" or "-BETA" in ref or "-RC" in ref


def matrix(refs=None, remote: str = REPO) -> list[dict]:
    """Check every release line. Returns one row per ref."""
    rows = []
    for ref in (refs or discover_refs(remote)):
        result = check(lambda p, r=ref: _fetch(r, p))
        rows.append({
            "ref": ref,
            "unreleased": is_unreleased(ref),
            "modules": result,
        })
    return rows


def _verdict(r: dict) -> str:
    if r["native"]:
        return "native"
    return "ok" if r["ok"] else "BROKEN"


#: Versions a human has actually run a backup on, with real data, on real hardware.
#: This is NOT automatable and must never be inferred: everything else in this file
#: is static analysis of iX's source, which proves the patch's assumptions hold --
#: a strictly weaker claim than "a restore worked". Add a row only after doing it.
HARDWARE_VERIFIED = {
    "25.10.4": "nested + providers; 252-snapshot recursive backup of /mnt/Tap, 18m",
}

_LEGEND = """
| verdict | meaning |
| --- | --- |
| **ok** | Every assumption the patch makes about middleware still holds. |
| **BROKEN** | middleware changed underneath the patch. `apply.sh` **refuses to apply that module** on this version and leaves TrueNAS stock, so backups keep working — without the module's feature. |
| **native** | TrueNAS does this itself now. The module retires; it is not a failure. |

"ok" means *the patch's assumptions hold*, checked automatically against iX's
source. It does not mean a human ran a backup on it — that is the
**Hardware-verified** column, which is filled in by hand and only by doing it.
"""


def render_markdown(rows: list[dict]) -> str:
    """The matrix, for COMPATIBILITY.md and the README."""
    out = [
        "| TrueNAS | B2/S3 providers | Nested snapshots | Hardware-verified |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        m = row["modules"]
        ref = row["ref"]
        label = ref.removeprefix("TS-").removeprefix("release/")
        if row["unreleased"]:
            label = f"{label} _(unreleased)_"

        cells = []
        for mod in (PROVIDERS, NESTED):
            v = _verdict(m[mod])
            cells.append({
                "ok": "ok",
                "BROKEN": "**BROKEN**",
                "native": "native",
            }[v])

        version = ref.removeprefix("TS-")
        hw = HARDWARE_VERIFIED.get(version)
        out.append(f"| {label} | {cells[0]} | {cells[1]} | {hw or '—'} |")

    return "\n".join(out) + "\n" + _LEGEND


def render_matrix(rows: list[dict]) -> str:
    """A support table.

    Says "assumptions hold", not "works" -- this is static analysis of iX's source,
    which is a strictly weaker claim than having run a backup on the hardware. The
    hardware-verified column lives in COMPATIBILITY.md and is maintained by hand,
    because nothing else can honestly fill it in.
    """
    w = max((len(r["ref"]) for r in rows), default=10)
    lines = [
        f"{'TrueNAS'.ljust(w)}  {'providers':<10}  {'nested':<10}",
        f"{'-' * w}  {'-' * 10}  {'-' * 10}",
    ]
    for row in rows:
        m = row["modules"]
        lines.append(
            f"{row['ref'].ljust(w)}  "
            f"{_verdict(m[PROVIDERS]):<10}  {_verdict(m[NESTED]):<10}"
        )
    return "\n".join(lines)


# ── reporting ────────────────────────────────────────────────────────────────

def render(label: str, result: dict) -> str:
    lines = [f"TrueNAS middleware @ {label}", ""]
    for module, r in sorted(result.items()):
        if r["native"]:
            lines.append(
                f"  [NATIVE]  {module}: TrueNAS appears to support this natively "
                f"now — the module should be retired, not fixed."
            )
        elif r["ok"]:
            lines.append(f"  [ok]      {module}: all assumptions hold")
        else:
            lines.append(f"  [BROKEN]  {module}:")
            for p in r["problems"]:
                lines.append(f"              - {p['detail']}")
                lines.append(f"                why it matters: {p['why']}")
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ref", help="a truenas/middleware git ref, e.g. master")
    g.add_argument("--tree", help="path to an installed middlewared package")
    g.add_argument("--matrix", action="store_true",
                   help="check every TrueNAS release line, newest of each")
    ap.add_argument("--module", action="append", choices=[PROVIDERS, NESTED],
                    help="check only this module (repeatable)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", action="store_true",
                    help="with --matrix: emit the table for COMPATIBILITY.md")
    args = ap.parse_args(argv[1:])

    if args.matrix:
        rows = matrix()
        if args.json:
            print(json.dumps(rows, indent=2))
        elif args.markdown:
            print(render_markdown(rows))
        else:
            print(render_matrix(rows))
        # A broken UNRELEASED line (master, -BETA, -RC) is a warning, not a build
        # failure -- it is exactly what we want to know early, and it is iX's tree
        # to change. compat.yml turns it into a bug report. A broken SHIPPED line
        # is a genuine failure: users are on it right now.
        shipped_broken = [
            r["ref"] for r in rows
            if not r["unreleased"]
            and any(not m["ok"] and not m["native"] for m in r["modules"].values())
        ]
        if shipped_broken:
            print(f"\nBROKEN on shipped releases: {', '.join(shipped_broken)}",
                  file=sys.stderr)
            return 1
        return 0

    label = args.ref or args.tree
    result = (check_ref(args.ref, args.module) if args.ref
              else check_tree(args.tree, args.module))

    if args.json:
        print(json.dumps({"ref": label, "modules": result}, indent=2))
    else:
        print(render(label, result))

    # Exit 1 if any module is broken. "Native" is not broken -- it is good news.
    return 1 if any(not r["ok"] and not r["native"] for r in result.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
