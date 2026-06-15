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
import ssl
import sys
import urllib.error
import urllib.request


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
        ptype = (t.get("credentials") or {}).get("provider", {}).get("type", "?")
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
    print(f"Created task id={result['id']}  name={result['description']!r}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Manage TrueNAS TrueCloud Backup tasks (S3 / B2 / Storj)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples")[1] if "Examples" in __doc__ else "",
    )
    p.add_argument("--host", required=True, metavar="HOST",
                   help="TrueNAS hostname or IP address")
    p.add_argument("--api-key", required=True, metavar="KEY",
                   help="TrueNAS API key (System → API Keys)")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification (self-signed certs)")

    sub = p.add_subparsers(dest="cmd", required=True)

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
    client = make_client(args.host, args.api_key, args.insecure)

    dispatch = {
        "list-credentials": cmd_list_credentials,
        "list-tasks":       cmd_list_tasks,
        "create":           cmd_create,
    }
    dispatch[args.cmd](client, args)


if __name__ == "__main__":
    main()
