#!/usr/bin/env python3
"""
create_task.py — create TrueNAS TrueCloud Backup tasks with S3 or B2 credentials.

The TrueNAS UI normally restricts the credential dropdown to Storj only.
This script bypasses that restriction by talking to the TrueNAS middleware
directly via `midclt` (the /api/v2.0 REST API is removed in TrueNAS 26.04).

Compatible providers (after the truecloud-patch backend patch is applied):
  S3     — any S3-compatible endpoint (AWS, Wasabi, Cloudflare R2, MinIO, …)
  B2     — Backblaze B2 native API
  STORJ_IX — Storj (unchanged, always worked)

Run this ON the TrueNAS host — it uses the local middleware socket via `midclt`,
so no host address or API key is needed.

Examples
--------
List available cloud credentials:
    python3 create_task.py list-credentials

Create a task backed by a B2 credential (id=3):
    python3 create_task.py create \\
        --name "tank-to-b2" \\
        --path /mnt/tank/data \\
        --credential 3 \\
        --bucket my-bucket \\
        --folder backups/tank \\
        --password-stdin \\
        --keep-last 14
    (pipe the password in:  echo -n "s3cret" | python3 create_task.py create ... )

Create a task using an S3-compatible credential (Wasabi, R2, etc.):
    python3 create_task.py create \\
        --name "tank-to-wasabi" \\
        --path /mnt/tank/data \\
        --credential 5 \\
        --bucket my-bucket \\
        --folder backups \\
        --password-stdin

List existing TrueCloud Backup tasks:
    python3 create_task.py list-tasks
"""

import argparse
import calendar
import getpass
import json
import os
import subprocess
import sys
import time

__version__ = "0.6.1"

_PATCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATUS_FILE = os.path.join(_PATCH_DIR, "hook_status.json")


def midclt_call(method, *args):
    """Call a middleware method on the local host.

    Uses `truenas_api_client` -- the library that backs `midclt` itself -- rather
    than shelling out to `midclt`.

    This is a SECURITY requirement, not a style choice. `midclt call <method>
    <json>` puts its arguments in the process's **argv**, and `cloud_backup.create`
    carries the restic repository password. argv is world-readable via `ps`, so
    shelling out would expose the key to the entire backup repo to every local
    user for the duration of the call. Going through the client library keeps it
    in this process's memory.
    """
    try:
        from truenas_api_client import Client
    except ImportError:
        print(
            "ERROR: `truenas_api_client` not importable — run this script ON the\n"
            "       TrueNAS host. (It ships with midclt.)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with Client() as client:
            return client.call(method, *args)
    except Exception as exc:  # noqa: BLE001 - surface any middleware error verbatim
        # Never echo `args` here: for cloud_backup.create it contains the password.
        print(f"ERROR: {method}: {exc}", file=sys.stderr)
        sys.exit(1)


# ── Sub-commands ──────────────────────────────────────────────────────────────

def _middlewared_start_epoch():
    """Epoch timestamp of the running middlewared main process, or None."""
    try:
        # Partial path (S607) is fine here: this runs as root on TrueNAS, so an
        # attacker who can poison PATH already has root. Hard-coding a path would
        # be less portable (/bin vs /usr/bin) for no security gain.
        pid = int(subprocess.run(
            ["systemctl", "show", "--property=MainPID", "--value", "middlewared"],  # noqa: S607
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip())
        if pid <= 0:
            return None
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            stat = fh.read()
        # Field 22 (starttime, in clock ticks since boot); the comm field may
        # contain spaces, so split after the closing paren.
        start_ticks = float(stat.rsplit(")", 1)[1].split()[19])
        # Base on /proc/stat btime, not uptime: starttime ticks count from the
        # kernel boot, which uptime does not match inside containers.
        with open("/proc/stat", encoding="ascii") as fh:
            btime = next(float(line.split()[1]) for line in fh
                         if line.startswith("btime "))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration,
            subprocess.SubprocessError):
        return None


def cmd_verify():
    """Print the hook status written by apply.sh at boot."""
    if not os.path.exists(_STATUS_FILE):
        print("No hook status file found.")
        print("Either the patch has never loaded (middlewared not yet restarted")
        print("after install) or the status file was deleted.")
        print(f"  Expected: {_STATUS_FILE}")
        sys.exit(1)

    try:
        with open(_STATUS_FILE, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read status file: {exc}")
        print(f"  Path: {_STATUS_FILE}")
        print("Try restarting middlewared to regenerate it.")
        sys.exit(1)

    print(f"Hook status (recorded at {status.get('patched_at', 'unknown')})")
    print()
    all_ok = True
    any_active = False
    for module, info in status.get("patches", {}).items():
        ok = info.get("ok", False)
        # A module can be inactive because TrueNAS now does it natively, or
        # because it is opt-in and switched off. Neither is a failure.
        active = info.get("active", True)
        label = "OK  " if ok else "FAIL"
        if ok and not active:
            label = "SKIP"
        detail = f"  — {info['detail']}" if info.get("detail") else ""
        print(f"  [{label}] {module}{detail}")
        if not ok:
            all_ok = False
        if active:
            any_active = True

    # The disk status alone can false-positive: at boot the files are patched
    # while middlewared is already running with the stock modules imported.
    # The running process only has the patch if it started AFTER patched_at.
    try:
        patched_epoch = calendar.timegm(
            time.strptime(status.get("patched_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        patched_epoch = None
    mw_start = _middlewared_start_epoch()

    proc_stale = False
    if not any_active:
        # Nothing is patched into middlewared, so whether it restarted since is
        # irrelevant -- there is nothing for it to have loaded.
        print("  [--  ] running middlewared process — no active module; nothing to load")
    elif patched_epoch is None or mw_start is None:
        print("  [??  ] running middlewared process — could not compare start time;")
        print("         the results above reflect the on-disk state only")
    elif mw_start + 2 < patched_epoch:
        proc_stale = True
        print("  [FAIL] running middlewared process — started BEFORE the patch was applied,")
        print("         so it is running the stock (unpatched) modules")
    else:
        print("  [OK  ] running middlewared process — started after the patch was applied")

    print()
    if all_ok and not proc_stale:
        print("All patches installed. Run a test backup to confirm end-to-end.")
    elif all_ok:
        print("The patch is on disk but not loaded. Right after boot, the deferred")
        print("restart (unit truecloud-mw-restart) may still be pending — re-check in a")
        print("minute. Otherwise run: systemctl restart middlewared")
        sys.exit(1)
    else:
        print("One or more patches failed to apply.")
        print(f"Check {os.path.join(_PATCH_DIR, 'apply.log')} and journalctl -u middlewared")
        sys.exit(1)

def _provider_type(cred):
    """Provider type string across schemas (<=24.10 plain str, >=25.04 dict)."""
    p = (cred or {}).get("provider")
    if isinstance(p, dict):
        return p.get("type", "?")
    return p or "?"


def cmd_list_credentials(_args):
    creds = midclt_call("cloudsync.credentials.query")
    if not creds:
        print("No cloud credentials configured.")
        return
    print(f"{'ID':>4}  {'Provider':<14}  Name")
    print("─" * 55)
    for c in sorted(creds, key=lambda x: x["id"]):
        print(f"{c['id']:>4}  {_provider_type(c):<14}  {c['name']}")


def cmd_list_tasks(_args):
    tasks = midclt_call("cloud_backup.query")
    if not tasks:
        print("No TrueCloud Backup tasks configured.")
        return
    print(f"{'ID':>4}  {'Enabled':<8}  {'Provider':<14}  Name")
    print("─" * 60)
    for t in sorted(tasks, key=lambda x: x["id"]):
        ptype = _provider_type(t.get("credentials"))
        enabled = "yes" if t.get("enabled") else "no"
        print(f"{t['id']:>4}  {enabled:<8}  {ptype:<14}  {t.get('description', '')}")


def _resolve_password(args):
    """Get the restic repo password without writing it to the user's shell history.

    That password is the key to the whole backup repository. `--password <secret>`
    persists it in ~/.bash_history and exposes it in `ps` for the lifetime of the
    shell command, so it is accepted but warned about; stdin and an interactive
    prompt are the safe paths.
    """
    if args.password_stdin:
        if args.password:
            print("ERROR: use either --password or --password-stdin, not both.",
                  file=sys.stderr)
            sys.exit(1)
        password = sys.stdin.readline().rstrip("\n")
    elif args.password:
        print(
            "WARNING: --password puts the restic repository password in your shell\n"
            "         history. Prefer:  echo -n 'pw' | ... --password-stdin",
            file=sys.stderr,
        )
        password = args.password
    else:
        password = getpass.getpass("Restic repository password: ")

    if not password:
        print("ERROR: the restic repository password must not be empty.",
              file=sys.stderr)
        sys.exit(1)
    return password


def cmd_create(args):
    parts = args.schedule.split()
    if len(parts) != 5:
        print(
            "ERROR: --schedule must be a 5-field cron expression, e.g. '0 2 * * *'",
            file=sys.stderr,
        )
        sys.exit(1)
    minute, hour, dom, month, dow = parts

    password = _resolve_password(args)

    body = {
        "description": args.name,
        "path": args.path,
        "credentials": args.credential,
        "attributes": {
            "bucket": args.bucket,
            "folder": args.folder,
        },
        "password": password,
        "keep_last": args.keep_last,
        "transfer_setting": args.transfer_setting,
        "schedule": {
            "minute": minute,
            "hour": hour,
            "dom": dom,
            "month": month,
            "dow": dow,
        },
        "snapshot": args.snapshot,
        "absolute_paths": args.absolute_paths,
        "enabled": not args.disabled,
    }

    if args.cache_path:
        body["cache_path"] = args.cache_path
    else:
        print(
            "WARNING: no --cache-path given. TrueNAS will run restic with --no-cache, "
            "which is very slow for large repositories (it re-reads all repo metadata "
            "from the provider every run). Set --cache-path to a writable dir on a pool "
            "with free space.",
            file=sys.stderr,
        )

    result = midclt_call("cloud_backup.create", body)
    try:
        print(f"Created task id={result['id']}  name={result['description']!r}")
    except (KeyError, TypeError):
        print(f"Task created but response schema was unexpected: {result}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Manage TrueNAS TrueCloud Backup tasks (S3 / B2 / Storj)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples")[1] if __doc__ and "Examples" in __doc__ else "",
    )
    p.add_argument("--version", "-V", action="version", version=f"truecloud-patch {__version__}")
    # Deprecated & ignored: the tool now uses the local middleware via `midclt` (the
    # /api/v2.0 REST API is removed in TrueNAS 26.04), so it must run ON the TrueNAS
    # host and needs no host/API key. Kept accepted-but-ignored for compatibility.
    p.add_argument("--host", default=None, help=argparse.SUPPRESS)
    p.add_argument("--api-key", default=None, help=argparse.SUPPRESS)
    p.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify",           help="Check that the backend hook loaded correctly")
    sub.add_parser("list-credentials", help="List configured cloud credentials")
    sub.add_parser("list-tasks",       help="List TrueCloud Backup tasks")

    c = sub.add_parser("create", help="Create a new TrueCloud Backup task")
    c.add_argument("--name",        required=True,
                   help="Task description shown in the UI")
    c.add_argument("--path",        required=True,
                   help="Local dataset path (e.g. /mnt/tank/data)")
    c.add_argument("--credential",  required=True, type=int, metavar="ID",
                   help="Cloud credential ID — get it from list-credentials")
    c.add_argument("--bucket",      required=True,
                   help="Bucket (S3) or container (B2) name")
    c.add_argument("--folder",      default="",
                   help="Path within the bucket (default: root)")
    c.add_argument("--password",    default=None,
                   help="Restic repository password. UNSAFE: it lands in your shell "
                        "history. Prefer --password-stdin, or omit both and be prompted.")
    c.add_argument("--password-stdin", action="store_true",
                   help="Read the restic repository password from stdin (recommended)")
    c.add_argument("--keep-last",   type=int, default=14, metavar="N",
                   help="Snapshots to retain after each run (default: 14)")
    c.add_argument("--schedule",    default="0 2 * * *",
                   help="Cron schedule (default: '0 2 * * *' — daily at 02:00)")
    c.add_argument("--cache-path",  default="", metavar="PATH",
                   help="restic cache directory (e.g. /mnt/pool/.restic-cache). "
                        "STRONGLY recommended: without it TrueNAS runs restic with "
                        "--no-cache, which re-fetches all repo metadata from the "
                        "provider every run and is extremely slow on large repos.")
    c.add_argument("--transfer-setting",
                   choices=["DEFAULT", "PERFORMANCE", "FAST_STORAGE"],
                   default="DEFAULT",
                   help="Pack-size / concurrency preset (default: DEFAULT)")
    c.add_argument("--snapshot",    action="store_true",
                   help="Create a ZFS snapshot before each backup run")
    c.add_argument("--absolute-paths", action="store_true",
                   help="Preserve absolute paths inside the restic repository")
    c.add_argument("--disabled",    action="store_true",
                   help="Create the task in a disabled state")

    args = p.parse_args()

    if args.cmd == "verify":
        cmd_verify()
        return

    if args.host or args.api_key or args.insecure:
        print("NOTE: --host/--api-key/--insecure are deprecated and ignored; this tool "
              "now uses the local middleware (midclt) and must run on the TrueNAS host.",
              file=sys.stderr)

    if args.cmd == "list-credentials":
        cmd_list_credentials(args)
    elif args.cmd == "list-tasks":
        cmd_list_tasks(args)
    elif args.cmd == "create":
        cmd_create(args)


if __name__ == "__main__":
    main()
