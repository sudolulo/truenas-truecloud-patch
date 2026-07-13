# Creating a task from the CLI

> Part of [truenas-truecloud-patch](../README.md).

## Creating a task via CLI

If the UI still shows only Storj after refreshing (e.g. the JS bundle pattern
changed in a new TrueNAS version), create tasks directly. Run this **on the
TrueNAS host** — it talks to the local middleware via `midclt`, so it needs no
host address or API key:

```bash
# Replace /mnt/tank/truenas-truecloud-patch with your clone path

# List your cloud credentials to find the right ID
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py list-credentials

# Create a task with a B2 credential (id=3).
# The restic repo password is read from stdin, so it never lands in your shell
# history — nor in any process's argv, where `ps` would expose it.
printf '%s' 'restic-repo-password' | \
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py create \
    --name "tank-to-b2" \
    --path /mnt/tank/data \
    --credential 3 \
    --bucket my-bucket \
    --folder backups/tank \
    --password-stdin \
    --cache-path /mnt/tank/.restic-cache \
    --keep-last 14
```

Omit `--password-stdin` and you'll be prompted for the password instead. `--password
<secret>` still works but warns: that password is the encryption key for the whole
repository, and a CLI argument persists in your shell history forever.

> **Always pass `--cache-path`.** Without it TrueNAS runs restic with `--no-cache`,
> which re-fetches all repo metadata from the provider every run — glacially slow
> on large repos. Point it at a writable dir on a pool with free space.

> Versions ≤ 0.1.0 used the `/api/v2.0` REST API with `--host`/`--api-key`; those
> flags are now accepted-but-ignored (REST is removed in TrueNAS 26.04).

---

