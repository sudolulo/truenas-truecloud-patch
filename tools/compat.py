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
import hashlib
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
                 is_async=None, params=None, forwards=False, why=""):
        self.id = ident
        self.module = module
        self.path = path
        self.symbol = symbol
        self.kind = kind
        self.is_async = is_async
        #: The positional parameters the patch passes, in order.
        self.params = params or []
        #: True if the wrapper takes *args/**kwargs and forwards the rest. Then a
        #: trailing parameter that iX adds or removes is harmless, and only the
        #: leading `params` must still match.
        self.forwards = forwards
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
    # is_async is deliberately NOT asserted on these three. The patch now injects an
    # async OR a sync wrapper to match whichever the installed middleware declares
    # (TrueNAS <= 25.10 is async; 26 rewrote them synchronous), so asyncness is a
    # thing to DETECT, not a thing to require -- see async_flavour(). What must still
    # hold is the shape: same name, same leading positional parameters.
    Assumption(
        "create-snapshot", NESTED, "plugins/cloud/snapshot.py", "create_snapshot",
        params=["middleware", "path", "name"],
        why="SNAPSHOT_BLOCK wraps it and returns (snapshot, staging_root) instead of "
            "(snapshot, snap_path)",
    ),
    Assumption(
        "crud-mixin-validate", NESTED, "plugins/cloud/crud.py",
        "CloudTaskServiceMixin._validate",
        kind="method", params=["self", "app", "verrors", "name", "data"],
        why="CRUD_BLOCK wraps it to drop the no-further-nesting error",
    ),
    Assumption(
        # SYNC_BLOCK's wrapper is (middleware, job, cloud_backup, *args, **kwargs) and
        # forwards the rest, precisely because iX keeps changing the tail: 24.10 and
        # 25.04 have `(…, dry_run)`, 25.10 added `rate_limit`. Only the leading three
        # are named by the patch, so only they have to hold.
        "restic-backup", NESTED, "plugins/cloud_backup/sync.py", "restic_backup",
        forwards=True,
        params=["middleware", "job", "cloud_backup"],
        why="SYNC_BLOCK wraps it to tear down bind mounts in a finally",
    ),
    Assumption(
        # Not a plugin method -- a method on the middleware OBJECT itself, which the
        # manifest had no way to express and therefore never checked.
        #
        # The nested module calls `middleware.get_service(<ns>)` to decide whether to
        # sweep snapshots through `pool.snapshot` or `zfs.snapshot` (see
        # SNAPSHOT_SERVICES). If it ever disappears, `_can_delete()` catches the
        # AttributeError, reports BOTH namespaces unusable, and every nested backup
        # fails -- loudly, but only at RUN time, on a box the preflight had already
        # declared healthy. Checking it costs one file read.
        "get-service", NESTED, "utils/plugins.py",
        "LoadPluginsMixin.get_service", kind="method",
        params=["self", "name"],
        why="snapshot_service() resolves the snapshot namespace through it; without "
            "it the module cannot sweep the snapshot it just took",
    ),
]


def accepted_spellings(name):
    """The method names that satisfy a call to `<namespace>.<name>`.

    A CRUDService exposes `create`/`update`/`delete` from methods NAMED
    `do_create`/`do_update`/`do_delete`. Both are live across the matrix: 24.10 and
    25.04 declare `do_delete`, 25.10 renamed it to `delete`, and all of them answer
    to `<ns>.delete`. Accepting only the literal name reported working releases as
    BROKEN and would have switched nested snapshots off on boxes where they work.
    """
    return (name, f"do_{name}")


#: The spellings that satisfy `<ns>.delete`. A test binds this to the runtime's
#: `truecloud_nested.DELETE_METHODS`, so the checker and the patch cannot come to
#: disagree about what "can delete" means on the same box.
DELETE_NAMES = accepted_spellings("delete")


class MiddlewareCall:
    """A middlewared METHOD the injected code calls at runtime.

    THIS CLASS OF ASSUMPTION IS WHY THE CHECKER EXISTS, AND IT WAS THE ONE MISSING.

    The manifest above records the symbols the patch *wraps*. It said nothing about
    the methods the patch *calls* -- and that gap hid two separate TrueNAS 26 breaks
    that both pass every other check:

      * `get_dataset_recursive()` was deleted from plugins/cloud/snapshot.py, and the
        injected block called it out of the host module's namespace (now vendored).
      * plugins/zfs_/dataset.py and plugins/zfs_/snapshot.py were DELETED outright,
        taking `zfs.dataset.query`, `zfs.snapshot.query` and `zfs.snapshot.delete`
        with them. 26 uses filesystem.statfs and zfs.resource.* instead.

    Nothing about the five cloud_backup files reveals that. The patch would apply
    perfectly, and then the FIRST BACKUP would fail -- or, far worse, succeed at
    snapshotting and fail at `zfs.snapshot.delete`, orphaning one snapshot per
    descendant dataset (250 on a real pool) on every single run, forever.

    A method is present when some plugin file declares its namespace AND defines it.
    If iX merely MOVES a method to a different file we report BROKEN wrongly, and the
    module declines to apply -- costing a feature, not a backup. That asymmetry is
    the whole design: declining is always the cheaper mistake.
    """

    def __init__(self, ident, module, method, path, why="", also=()):
        self.id = ident
        self.module = module
        self.method = method              # "pool.snapshot.delete"
        self.path = path                  # plugin file that declares it
        self.why = why
        #: Equally acceptable spellings of the SAME call, as (method, path) pairs.
        #:
        #: No single snapshot namespace spans every supported release. 24.10 and
        #: 25.04 expose the CRUD service as the public `zfs.snapshot`; 25.10
        #: promoted it to `pool.snapshot` and demoted `zfs.snapshot` to private;
        #: 26 deleted `plugins/zfs_/` entirely. Pinning either one alone marks
        #: half the matrix BROKEN and declines to apply on versions that work
        #: perfectly well.
        #:
        #: The call is satisfied if ANY option is present. The runtime picks the
        #: same way -- see `pick_snapshot_service()` in the nested module -- so
        #: what this checks and what the patch does cannot drift apart.
        self.also = tuple(also)

    @property
    def options(self):
        """Every (method, path) that would satisfy this call, best first."""
        return ((self.method, self.path), *self.also)

    @staticmethod
    def namespace_of(method):
        return method.rsplit(".", 1)[0]

    @staticmethod
    def name_of(method):
        return method.rsplit(".", 1)[1]


#: Every middlewared method the nested module calls at runtime.
#: The middleware methods the nested module CALLS.
#:
#: These used to be the PRIVATE `zfs.*` service (`zfs.dataset.query`,
#: `zfs.snapshot.delete`, `zfs.snapshot.query`). TrueNAS 26 deleted
#: `plugins/zfs_/` outright and every one of them vanished -- silently, because a
#: private service carries no stability contract and nothing warned us. The patch
#: would have applied cleanly and then failed on the first backup.
#:
#: The replacements are the PUBLIC `pool.*` API, and switching to it is not merely
#: a TrueNAS 26 fix -- it is the correct call on every version:
#:
#:   * It is public, documented, and covered by iX's deprecation policy, so it
#:     cannot be deleted from under us the way `zfs.*` just was.
#:   * The same methods, in the same files, exist on 24.10 through 26. One code
#:     path, no version conditionals.
#:   * Both spellings take `recursive`, so ONE call sweeps the whole tree instead
#:     of ~250 individual deletes, any of which could be missed.
MIDDLEWARE_CALLS = [
    MiddlewareCall(
        "call-snapshot-delete", NESTED, "pool.snapshot.delete",
        "plugins/pool_/snapshot.py",
        also=[("zfs.snapshot.delete", "plugins/zfs_/snapshot.py")],
        why="delete_snapshot_tree() sweeps the recursive snapshot. Without it every "
            "run orphans one snapshot per descendant dataset (250 on a real pool)",
    ),
]

# There is deliberately NO entry here for a dataset or snapshot QUERY.
#
# The patch used to call `zfs.dataset.query` / `zfs.snapshot.query` (private, and
# deleted in TrueNAS 26). The obvious port was to the public `pool.dataset.query` /
# `pool.snapshot.query` -- and that port was WRONG in a way no source check could
# ever have caught, because the methods are all present and correctly shaped.
#
# They are simply filtered. On a real box they return 205 of 274 datasets and 205
# of 274 snapshots, hiding `ix-apps/*`, `.system/*` and `.ix-virt/*` -- 84 of 270
# on the production pool, including live application data. Staging from that view
# silently omits them; sweeping from it orphans one snapshot per hidden dataset,
# forever.
#
# So the module enumerates from ZFS itself and there is no middleware assumption
# left to check. That is the point: the fewer things we assume about middleware,
# the less there is for iX to break. Only the MUTATION is still a middleware call,
# and that is the one entry above.


def check_call(c: MiddlewareCall, src: str | None,
               method: str | None = None, path: str | None = None,
               ) -> tuple[str, str | None]:
    """Is `method` still registered by middlewared?

    `method`/`path` name WHICH spelling of the call is being tried -- a call may
    have several equally acceptable ones (see MiddlewareCall.also). They default
    to the preferred spelling.
    """
    method = method or c.method
    path = path or c.path
    namespace = MiddlewareCall.namespace_of(method)
    name = MiddlewareCall.name_of(method)

    if src is None:
        return "broken", (
            f"{path} no longer exists, so `{method}` is gone"
        )

    try:
        tree = ast.parse(_stock(src))
    except SyntaxError as e:
        return "unknown", f"{path} does not parse: {e}"

    # Find the CLASS that declares this namespace, and look for the method THERE.
    #
    # Not anywhere in the file. `ast.walk` over the whole module made *any* function
    # called `delete` satisfy the check -- one on an unrelated class, or even a nested
    # local function inside `do_query`. That is a FALSE OK, and it breaks the one
    # invariant this checker and the runtime share: `_defines_delete()` looks in
    # `vars(klass)` for a PLUGIN class on the service's MRO. If iX gutted
    # `PoolSnapshotService.do_delete` while some other class in the same file still had
    # a `delete`, compat would say ok, apply.sh would patch, and the runtime would then
    # correctly refuse `pool.snapshot`, fall through to a `zfs.snapshot` that does not
    # exist on 26, and fail every nested backup on a box the preflight called healthy.
    #
    # Same question on both sides: does the class that OWNS this namespace define the
    # method?
    #
    # A CRUDService exposes `create`/`update`/`delete` from methods NAMED
    # `do_create`/`do_update`/`do_delete`. Both spellings are live: 24.10 and 25.04
    # declare `do_delete`, 25.10 renamed it to `delete`, and all answer to
    # `<ns>.delete`. Accepting only the literal name reported working releases as
    # broken.
    owners = []
    all_namespaces = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        declared = {
            n.value.value
            for n in ast.walk(cls)
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
            and any(isinstance(t, ast.Name) and t.id == "namespace" for t in n.targets)
        }
        all_namespaces |= declared
        if namespace in declared:
            owners.append(cls)

    if not owners:
        return "broken", (
            f"{path} no longer declares namespace {namespace!r} "
            f"(found: {sorted(all_namespaces) or 'none'}), so `{method}` is gone"
        )

    wanted = accepted_spellings(name)
    for cls in owners:
        # Direct members of the class, not its nested scopes: a `def delete` inside
        # another method is a local function, not a service method.
        if any(
            isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in wanted
            for n in cls.body
        ):
            return "ok", None

    return "broken", (
        f"{path} still declares namespace {namespace!r}, but its class no longer "
        f"defines `{'` or `'.join(wanted)}` -- so `{method}` is gone"
    )


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

_DEFS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _defs_in(body):
    """Definitions in `body`, descending into if/try/else/with.

    A module-level `def` is not always at module level:

        try:
            from .fast import create_snapshot
        except ImportError:
            async def create_snapshot(...): ...

    Scanning only `tree.body` would say "no longer defines create_snapshot" -- a
    false BROKEN. And a false BROKEN is not a harmless over-caution here: it makes a
    module decline to apply on a box where it works perfectly.
    """
    for node in body:
        if isinstance(node, _DEFS):
            yield node
        elif isinstance(node, ast.If | ast.Try | ast.With | ast.AsyncWith):
            yield from _defs_in(node.body)
            yield from _defs_in(getattr(node, "orelse", []))
            yield from _defs_in(getattr(node, "finalbody", []))
            for h in getattr(node, "handlers", []):
                yield from _defs_in(h.body)


def _imports(tree, name):
    """True if `name` is bound by an import -- i.e. re-exported from elsewhere."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
    return False


def _find(tree, symbol):
    """The def node for `name` or `Class.method`, or None."""
    if "." in symbol:
        cls_name, meth = symbol.split(".", 1)
        for node in _defs_in(tree.body):
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for sub in _defs_in(node.body):
                    if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) \
                            and sub.name == meth:
                        return sub
        return None

    for node in _defs_in(tree.body):
        if node.name == symbol:
            return node
    return None


def _positional(node):
    a = node.args
    return [p.arg for p in (*a.posonlyargs, *a.args)]


def _signature_problem(node, symbol, want, forwards=False):
    """Why `symbol`'s signature no longer supports how the patch calls it.

    The injected blocks call the original POSITIONALLY and with a fixed arg list:

        await _tc_orig_create_snapshot(middleware, path, name)
        _tc_orig_get_restic_config(cloud_backup)

    So a name-subset test ("are these names still in there somewhere?") is not
    enough, and that is what this used to be. It passed a reorder, a keyword-only
    conversion, and an added required parameter -- each of which is a TypeError or,
    worse, silently correct-looking with the arguments swapped.

    The realistic one is not hypothetical: on `master`, iX already renamed
    get_restic_config's parameter and added a second. That function is rebound
    module-wide by RESTIC_BLOCK, so a wrong wrapper there kills EVERY TrueCloud
    task -- Storj included, for users who never wanted this patch's features.
    """
    have = _positional(node)
    n = len(want)

    if have[:n] != want:
        return (
            f"{symbol}{tuple(have)} — positional parameters changed; the patch "
            f"calls it as ({', '.join(want)})"
        )

    # Extra parameters are fine only if they are optional -- the patch will not pass
    # them -- OR if the wrapper forwards *args/**kwargs, in which case whatever the
    # caller supplied is handed straight through. A new REQUIRED one that we neither
    # pass nor forward is a TypeError at the first backup.
    args = node.args
    required = len(have) - len(args.defaults)
    if required > n and not forwards:
        return (
            f"{symbol} now requires {', '.join(have[n:required])} — the patch does "
            f"not pass it"
        )

    req_kwonly = [
        k.arg for k, d in zip(args.kwonlyargs, args.kw_defaults, strict=False)
        if d is None
    ]
    if req_kwonly and not forwards:
        return (
            f"{symbol} now requires keyword-only {', '.join(req_kwonly)} — the "
            f"patch does not pass it"
        )

    return None


def check_source(a: Assumption, src: str | None) -> tuple[str, str | None]:
    """("ok"|"broken"|"unknown", detail).

    "unknown" exists so that "I cannot inspect this" is never reported as "this is
    broken". Only "broken" makes a module decline to apply, and declining wrongly
    breaks a box that was working.
    """
    if src is None:
        # Name the SYMBOL, not just the file. Whoever reads the bug report needs to
        # know what the patch can no longer reach, and "utils/plugins.py does not
        # exist" does not tell them that `get_service` is gone.
        return "broken", f"{a.path} does not exist, so `{a.symbol}` is gone"

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return "unknown", f"{a.path} does not parse: {e}"

    node = _find(tree, a.symbol)
    if node is None:
        root = a.symbol.split(".", 1)[0]
        if _imports(tree, root):
            # Re-exported: `from ._impl import get_restic_config`. The name is still
            # there and the patch's rebinding still works; we simply cannot see the
            # signature from here. Refusing to apply over a refactor that changed
            # nothing would be worse than not checking.
            return "unknown", (
                f"{a.path} re-exports {root} from another module; "
                f"cannot verify its signature here"
            )
        return "broken", f"{a.path} no longer defines {a.symbol}"

    if a.kind == "class":
        if not isinstance(node, ast.ClassDef):
            return "broken", f"{a.symbol} is no longer a class"
        return "ok", None

    if isinstance(node, ast.ClassDef):
        return "broken", f"{a.symbol} is a class, expected a function"

    got_async = isinstance(node, ast.AsyncFunctionDef)
    if a.is_async is not None and got_async != a.is_async:
        want = "async def" if a.is_async else "def"
        got = "async def" if got_async else "def"
        return "broken", (
            f"{a.symbol} is now `{got}`, the patch requires `{want}` ({a.path})"
        )

    problem = _signature_problem(node, a.symbol, a.params, a.forwards)
    return ("broken", problem) if problem else ("ok", None)


# ── sources ──────────────────────────────────────────────────────────────────

class Unreadable(Exception):
    """The source could not be READ. That is not the same as it not existing.

    Folding these together is how a network blip becomes "iX deleted six files",
    which becomes "both modules are broken", which becomes a bug report, a red
    support matrix pushed to the README, and -- on a real box -- a module declining
    to apply. A transient failure must never be able to say anything about
    middleware.
    """


def _fetch(ref: str, path: str) -> str | None:
    """Source at `ref`, None if iX genuinely does not have that file (404).

    Raises Unreadable for anything else: rate limits (the matrix makes ~30
    unauthenticated requests per run and 429 is a real outcome), DNS, timeouts.
    """
    url = RAW.format(ref=ref, path=path)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:  # noqa: S310
            if r.status == 404:
                return None
            if r.status != 200:
                raise Unreadable(f"{url} -> HTTP {r.status}")
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None          # the file really is gone
        raise Unreadable(f"{url} -> HTTP {e.code}") from e
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(f"{url} -> {e!r}") from e


def _read(root: str, path: str) -> str | None:
    full = os.path.join(root, *path.split("/"))
    try:
        with open(full, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None                       # genuinely absent
    except OSError as e:
        raise Unreadable(f"{full} -> {e!r}") from e   # permissions, I/O, ...


def _stock(text: str) -> str:
    """Only the part of the file that iX wrote.

    Our own blocks are appended after the MARKER, and they quote the very strings
    the probes look for -- B2_BLOCK literally writes `B2RcloneRemote.restic = True`
    into b2.py, and CRUD_BLOCK quotes the "no further nesting" message it filters
    on. Scanning the whole file on an already-patched box therefore finds OUR text
    and concludes TrueNAS went native, i.e. "retire the module". apply.sh has always
    cut at the marker for exactly this reason; compat.py did not, so the one command
    its own docstring recommends for a live box (`--tree /usr/lib/.../middlewared`)
    reported providers as native on every patched machine.
    """
    return text.split("\n# TRUECLOUD_PATCH", 1)[0]


def check(loader, modules=None) -> dict:
    """Check every assumption. `loader(path) -> source|None`, may raise Unreadable.

    Returns {module: {"ok", "native", "unknown", "problems"}}.

    `unknown` means the sources could not be READ -- a rate limit, a timeout, an
    unreadable tree. It is NOT `ok` and it is emphatically NOT `broken`: nothing may
    act on a verdict derived from a failed download.
    """
    modules = modules or [PROVIDERS, NESTED]
    cache = {}

    def src(path):
        if path not in cache:
            cache[path] = loader(path)
        return cache[path]

    out = {
        m: {"ok": True, "native": False, "unknown": False, "problems": []}
        for m in modules
    }

    for a in ASSUMPTIONS:
        if a.module not in out:
            continue
        try:
            text = src(a.path)
        except Unreadable as e:
            out[a.module]["unknown"] = True
            out[a.module]["problems"].append({
                "id": a.id, "detail": f"could not read {a.path}: {e}", "why": a.why,
                "state": "unknown",
            })
            continue

        status, detail = check_source(a, text if text is None else _stock(text))
        if status == "broken":
            out[a.module]["ok"] = False
            out[a.module]["problems"].append({
                "id": a.id, "detail": detail, "why": a.why, "state": "broken",
            })
        elif status == "unknown":
            out[a.module]["unknown"] = True
            out[a.module]["problems"].append({
                "id": a.id, "detail": detail, "why": a.why, "state": "unknown",
            })

    # The methods the injected code CALLS, not just the symbols it wraps.
    #
    # A call may have several equally acceptable spellings, because no single
    # snapshot namespace spans every supported release (24.10 has `zfs.snapshot`,
    # 26 has only `pool.snapshot`). It is satisfied if ANY of them is present --
    # exactly as the runtime resolves it -- and BROKEN only when they all vanish.
    for c in MIDDLEWARE_CALLS:
        if c.module not in out:
            continue

        satisfied, unknown, details = False, False, []
        for method, path in c.options:
            try:
                text = src(path)
            except Unreadable as e:
                unknown = True
                details.append(f"could not read {path}: {e}")
                continue

            status, detail = check_call(c, text, method, path)
            if status == "ok":
                satisfied = True
                break
            if status == "unknown":
                unknown = True
            details.append(detail)

        if satisfied:
            continue

        # Every spelling failed. If we could not READ one of them we do not know
        # that it is broken -- a rate-limited fetch is not a regression.
        if unknown:
            out[c.module]["unknown"] = True
            out[c.module]["problems"].append({
                "id": c.id, "detail": "; ".join(details), "why": c.why,
                "state": "unknown",
            })
        else:
            out[c.module]["ok"] = False
            out[c.module]["problems"].append({
                "id": c.id, "detail": "; ".join(details), "why": c.why,
                "state": "broken",
            })

    for module, (path, phrase, native_when_present) in NATIVE_PROBES.items():
        if module not in out:
            continue
        try:
            text = src(path)
        except Unreadable:
            out[module]["unknown"] = True
            continue
        if text is None:
            continue
        present = _squash(phrase) in _squash(_stock(text))
        out[module]["native"] = (present == native_when_present)

    # `ok` is cleared ONLY by a definite violation, so "unknown" never needs to
    # repair it -- and must not: a module with one unreadable file AND one proven
    # broken assumption is broken, not unknown.
    return out


#: The three stock symbols the nested module wraps. TrueNAS <= 25.10 declares them
#: `async def`; TrueNAS 26 rewrote them synchronous. apply.sh injects the wrapper
#: that matches, so this is the question it has to answer at every boot.
NESTED_WRAPPED = [
    ("plugins/cloud/snapshot.py", "create_snapshot"),
    ("plugins/cloud/crud.py", "CloudTaskServiceMixin._validate"),
    ("plugins/cloud_backup/sync.py", "restic_backup"),
]


def async_flavour(loader) -> bool | None:
    """Is the installed cloud_backup path async? True, False, or None if unclear.

    None means "do not patch": either a symbol is missing, or -- the case worth
    naming -- the three DISAGREE. A middleware caught half-converted is one this
    patch has never seen, and guessing a flavour there means injecting an `async def`
    that a synchronous caller unpacks as a tuple. Declining costs a feature; guessing
    costs a backup.
    """
    flavours = set()
    for path, symbol in NESTED_WRAPPED:
        try:
            src = loader(path)
        except Unreadable:
            return None
        if src is None:
            return None
        try:
            tree = ast.parse(_stock(src))
        except SyntaxError:
            return None

        node = _find(tree, symbol)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return None
        flavours.add(isinstance(node, ast.AsyncFunctionDef))

    return flavours.pop() if len(flavours) == 1 else None


def async_flavour_tree(root: str) -> bool | None:
    return async_flavour(lambda p: _read(root, p))


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
    """BROKEN outranks native, which outranks unknown.

    "native" used to win outright, which meant a module that was BOTH broken and
    apparently-native rendered as good news: green CI, no bug report, and a README
    row telling users the feature went native while it was in fact broken. A proven
    violation is the strongest signal here and must never be masked by a weaker one
    -- and the native probe is only a substring match on iX's source, so it is
    exactly the weaker one.
    """
    if not r["ok"]:
        return "BROKEN"
    if r["native"]:
        return "native"
    if r["unknown"]:
        return "unknown"
    return "ok"


def is_broken(r: dict) -> bool:
    return not r["ok"]


#: Versions a human has actually run a backup on, with real data, on real hardware.
#: This is NOT automatable and must never be inferred: everything else in this file
#: is static analysis of iX's source, which proves the patch's assumptions hold --
#: a strictly weaker claim than "a restore worked". Add a row only after doing it.
HARDWARE_VERIFIED = {
    "25.10.4": (
        "v0.7.0: 3 live tasks — 191-dataset nested backup of /mnt/Tap, a "
        "215-filesystem/2-zvol backup of /mnt/Tank/backups, and a non-nested one; "
        "0 orphans, 0 leaked mounts, byte-identical restore; the collector also "
        "reclaimed a real orphan the pool had been carrying"
    ),
    "26.0.0-BETA.1": (
        "v0.7.0: 274-snapshot recursive backup of a 292-dataset pool; restored a "
        "4-deep child dataset byte-identical; zvol-orphan case reproduced then closed"
    ),
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


#: The README's matrix lives between these. CI regenerates it daily, so a table
#: claiming the patch works on a TrueNAS that iX has since changed cannot survive
#: for longer than a day -- a stale support matrix is not a stale doc, it is a lie
#: to somebody deciding whether to trust this with their backups.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT_DIR, "README.md")
BEGIN = "<!-- BEGIN COMPAT MATRIX (generated by tools/compat.py --matrix --markdown) -->"
END = "<!-- END COMPAT MATRIX -->"


def update_readme(rows: list[dict], path: str = README) -> bool:
    """Rewrite the README's matrix block. True if it changed.

    Refuses if ANY row could not be fully checked. The published matrix is what a
    stranger reads before trusting this with their backups, and CI pushes it
    automatically -- so a rate limit or a DNS blip must never be able to repaint it.
    A stale-but-true table beats a fresh-but-invented one.
    """
    unknown = [
        r["ref"] for r in rows
        if any(m["unknown"] for m in r["modules"].values())
    ]
    if unknown:
        raise Unreadable(
            "not rewriting the matrix: could not fully check " + ", ".join(unknown)
        )

    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    i, j = text.find(BEGIN), text.find(END)
    if i == -1 or j == -1:
        raise ValueError(f"{path} has no COMPAT MATRIX markers")

    new = f"{BEGIN}\n{render_markdown(rows).rstrip()}\n{END}"
    old = text[i:j + len(END)]
    if old == new:
        return False

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text[:i] + new + text[j + len(END):])
    return True


def render_markdown(rows: list[dict]) -> str:
    """The matrix, for the README."""
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
                "unknown": "unknown",
            }[v])

        version = ref.removeprefix("TS-")
        hw = HARDWARE_VERIFIED.get(version)
        out.append(f"| {label} | {cells[0]} | {cells[1]} | {hw or '—'} |")

    return "\n".join(out) + "\n" + _LEGEND


FINGERPRINT = "<!-- compat-fingerprint:"


def fingerprint(rows: list[dict]) -> str:
    """A stable digest of WHAT IS BROKEN, and nothing else.

    The bug report must be updated when the findings change and stay silent when they
    do not. Without this the workflow commented on every run -- it left **11 identical
    3,000-character comments** on one issue in a single day, which is not a warning
    system, it is a mute button with extra steps.

    Deliberately excludes anything that moves on its own: the matrix's `ok` rows, the
    hardware-verified column, and the exact TrueNAS point-release (`TS-25.10.4` ->
    `TS-25.10.5` is not news). Only the broken (ref, module, problem-id) triples count.
    """
    findings = sorted(
        (r["ref"], mod, p["id"])
        for r in rows
        for mod, m in r["modules"].items()
        if is_broken(m)
        for p in m["problems"]
        # `unknown` problems are things we could not READ (a 429, an EACCES), not
        # things iX changed. On a ref that is broken for some other reason they would
        # otherwise join the digest, so one transient network blip rewrites the issue
        # body and the next clean run rewrites it back. That is the daily-noise
        # failure this fingerprint exists to prevent, wearing a different hat.
        if p.get("state", "broken") == "broken"
    )
    return hashlib.sha256(repr(findings).encode()).hexdigest()[:16]


def extract_fingerprint(body: str) -> str | None:
    """The fingerprint a previous run left in the issue body, if any."""
    if not body:
        return None
    i = body.find(FINGERPRINT)
    if i == -1:
        return None
    return body[i + len(FINGERPRINT):].split("-->", 1)[0].strip() or None


def render_issue(rows: list[dict]) -> str:
    """The bug report body: what is broken, why it matters, and nothing else up front.

    Short by design. The full matrix and the healthy versions go in a fold -- somebody
    opening this wants to know what broke and whether it can hurt them, not to re-read
    a table they can see in the README.
    """
    broken = [r for r in rows if any(is_broken(m) for m in r["modules"].values())]

    out = [
        "`tools/compat.py` checks what this patch assumes about middlewared against "
        "iXsystems' actual source, every day. Those assumptions no longer hold on the "
        "versions below.",
        "",
        "**This does not break anyone today.** `apply.sh` re-checks on every boot and "
        "**declines to apply** a module whose assumptions fail, so TrueNAS is left "
        "stock rather than half-patched. The cost is the module's feature, not a "
        "broken backup.",
        "",
    ]

    for r in broken:
        out.append(f"### `{r['ref']}`")
        out.append("")
        for mod, m in sorted(r["modules"].items()):
            if not is_broken(m):
                continue
            out.append(f"**{mod}**")
            out.append("")
            for p in m["problems"]:
                out.append(f"- {p['detail']}")
                out.append(f"  <br><sub>{p['why']}</sub>")
            out.append("")

    out += [
        "<details><summary>Full support matrix</summary>",
        "",
        render_markdown(rows),
        "</details>",
        "",
        "_Filed and kept up to date by "
        "[`compat.yml`](.github/workflows/compat.yml). It edits this body when the "
        "findings change, and stays quiet when they do not._",
        "",
        f"{FINGERPRINT} {fingerprint(rows)} -->",
    ]
    return "\n".join(out)


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
                    help="with --matrix: emit the table as markdown")
    ap.add_argument("--update-readme", action="store_true",
                    help="with --matrix: rewrite the README's matrix block in place")
    args = ap.parse_args(argv[1:])

    if args.matrix:
        rows = matrix()
        if args.json:
            print(json.dumps(rows, indent=2))
        elif args.markdown:
            print(render_markdown(rows))
        elif args.update_readme:
            changed = update_readme(rows)
            print("README.md updated" if changed else "README.md already current")
        else:
            print(render_matrix(rows))
        # A broken UNRELEASED line (master, -BETA, -RC) is a warning, not a build
        # failure -- it is exactly what we want to know early, and it is iX's tree
        # to change. compat.yml turns it into a bug report. A broken SHIPPED line
        # is a genuine failure: users are on it right now.
        shipped_broken = [
            r["ref"] for r in rows
            if not r["unreleased"]
            and any(is_broken(m) for m in r["modules"].values())
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
    return 1 if any(is_broken(r) for r in result.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
