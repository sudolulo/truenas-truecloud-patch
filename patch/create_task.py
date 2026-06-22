#!/usr/bin/env python3
"""
create_task.py — create TrueNAS TrueCloud Backup tasks with S3 or B2 credentials.

The TrueNAS UI normally restricts the credential dropdown to Storj only.
This script bypasses that restriction by calling the REST API directly.

Compatible providers (after the truecloud-patch backend patch is applied):
  S3     — any S3-compatible endpoint (AWS, Wasabi, Cloudflare R2, MinIO, …)
  B2     — Backblaze B2 native API
  STORJ_IX — Storj (unchanged, always worked)

Requires a TrueNAS API key: UI → System → API Keys → Add.

Examples
--------
List available cloud credentials:
    python3 create_task.py --host 192.168.1.1 --api-key <key> list-credentials

Create a task backed by a B2 credential (id=3):
    python3 create_task.py --host 192.168.1.1 --api-key <key> create \\
        --name "tank-to-b2" \\
        --path /mnt/tank/data \\
        --credential 3 \\
        --bucket my-bucket \\
        --folder backups/tank \\
        --password "restic-repo-password" \\
        --keep-last 14

Create a task using an S3-compatible credential (Wasabi, R2, etc.):
    python3 create_task.py --host 192.168.1.1 --api-key <key> create \\
        --name "tank-to-wasabi" \\
        --path /mnt/tank/data \\
        --credential 5 \\
        --bucket my-bucket \\
        --folder backups \\
        --password "restic-repo-password"

List existing TrueCloud Backup tasks:
    python3 create_task.py --host 192.168.1.1 --api-key <key> list-tasks
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

__version__ = "0.0.3"

_PATCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATUS_FILE = os.path.join(_PATCH_DIR, "hook_status.json")


def make_client(host, api_key, insecure=False):
    """Return a callable that makes authenticated REST API calls."""
    base = f"https://{host}/api/v2.0"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    def call(method, path, body=None):
        url = base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            print(f"HTTP {exc.code} {exc.reason}: {detail}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as exc:
            print(f"Connection error: {exc.reason}", file=sys.stderr)
            sys.exit(1)

    return call


# ── Sub-commands ──────────────────────────────────────────────────────────────

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
    for module, info in status.get("patches", {}).items():
        ok = info.get("ok", False)
        label = "OK  " if ok else "FAIL"
        detail = f"  — {info['detail']}" if info.get("detail") else ""
        print(f"  [{label}] {module}{detail}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("All patches installed. Run a test backup to confirm end-to-end.")
    else:
        print("One or more patches failed to apply.")
        print(f"Check {os.path.join(_PATCH_DIR, 'apply.log')} and journalctl -u middlewared")
        sys.exit(1)

def cmd_list_credentials(client, _args):
    creds = client("GET", "/cloudsync/credentials")
    if not creds:
        print("No cloud credentials configured.")
        return
    print(f"{'ID':>4}  {'Provider':<14}  Name")
    print("─" * 55)
    for c in sorted(creds, key=lambda x: x["id"]):
        print(f"{c['id']:>4}  {c['provider']['type']:<14}  {c['name']}")


def cmd_list_tasks(client, _args):
    tasks = client("GET", "/cloud_backup")
    if not tasks:
        print("No TrueCloud Backup tasks configured.")
        return
    print(f"{'ID':>4}  {'Enabled':<8}  {'Provider':<14}  Name")
    print("─" * 60)
    for t in sorted(tasks, key=lambda x: x["id"]):
        creds = t.get("credentials") or {}
        ptype = (creds.get("provider") or {}).get("type", "?")
        enabled = "yes" if t.get("enabled") else "no"
        print(f"{t['id']:>4}  {enabled:<8}  {ptype:<14}  {t.get('description', '')}")


def cmd_create(client, args):
    parts = args.schedule.split()
    if len(parts) != 5:
        print(
            "ERROR: --schedule must be a 5-field cron expression, e.g. '0 2 * * *'",
            file=sys.stderr,
        )
        sys.exit(1)
    minute, hour, dom, month, dow = parts

    body = {
        "description": args.name,
        "path": args.path,
        "credentials": args.credential,
        "attributes": {
            "bucket": args.bucket,
            "folder": args.folder,
        },
        "password": args.password,
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

    result = client("POST", "/cloud_backup", body)
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
    p.add_argument("--host", default=None, metavar="HOST",
                   help="TrueNAS hostname or IP address (required except for verify)")
    p.add_argument("--api-key", default=None, metavar="KEY",
                   help="TrueNAS API key — System → API Keys (required except for verify)")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification (self-signed certs). "
                        "WARNING: exposes your API key to network interception. "
                        "Prefer adding your cert to the trust store instead.")

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
    c.add_argument("--password",    required=True,
                   help="Restic repository encryption password (choose a strong one)")
    c.add_argument("--keep-last",   type=int, default=14, metavar="N",
                   help="Snapshots to retain after each run (default: 14)")
    c.add_argument("--schedule",    default="0 2 * * *",
                   help="Cron schedule (default: '0 2 * * *' — daily at 02:00)")
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

    if not args.host or not args.api_key:
        p.error("--host and --api-key are required for this command")

    client = make_client(args.host, args.api_key, args.insecure)
    if args.cmd == "list-credentials":
        cmd_list_credentials(client, args)
    elif args.cmd == "list-tasks":
        cmd_list_tasks(client, args)
    elif args.cmd == "create":
        cmd_create(client, args)


if __name__ == "__main__":
    main()
