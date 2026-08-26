# Recovery and troubleshooting

> Part of [truenas-truecloud-patch](../README.md).

## Emergency recovery

### middlewared won't start

Run this from the TrueNAS shell (local console, SSH, or the debug shell in
the UI):

```bash
bash /mnt/tank/truenas-truecloud-patch/recover.sh
```

Replace the path with your clone location. This creates a kill-switch file
(`disabled`) in the repo root, unmounts the overlay so the original files are
visible immediately, then restarts middlewared. No reboot required.

If you cannot run a script and only have a bare shell prompt:

```bash
touch /mnt/tank/truenas-truecloud-patch/disabled
systemctl restart middlewared
```

If you don't remember where you cloned the repo (midclt won't work while middlewared is
down), find the path two ways:

```bash
# Option 1 — search the filesystem:
find /mnt -name "recover.sh" -path "*/truenas-truecloud-patch/*" 2>/dev/null

# Option 2 — query the TrueNAS database directly:
sqlite3 /data/freenas-v1.db \
    "SELECT script FROM initshutdownscript WHERE comment = 'TrueCloud provider patch (S3/B2)';"
```

The `script` column shows the full path to `patch/apply.sh`; your clone root is one
level up (strip `/patch/apply.sh` from the end). Then run the `touch` command above
with that path.

If middlewared **still** won't start after the kill switch is set, the problem
is unrelated to this patch. Check:

```bash
journalctl -u middlewared -n 50
```

To re-enable the patch once you have investigated:

```bash
rm /mnt/tank/truenas-truecloud-patch/disabled
bash /mnt/tank/truenas-truecloud-patch/patch/apply.sh
systemctl restart middlewared   # manual apply.sh runs never restart for you
```

---

### Web UI is blank or broken

If the TrueNAS web interface loads blank or shows JavaScript errors, the
Angular bundle may have been interrupted mid-write (e.g. power cut during
boot). The original bundle is always backed up before patching, so recovery
is straightforward:

```bash
# Find the backup (the path varies by TrueNAS version):
find /usr/share/truenas /usr/share/truenas-ui /var/www/truenas -name "*.js.pre-truecloud-patch" 2>/dev/null

# Restore it — substitute the actual path from the find output:
mv /usr/share/truenas/webui/main.XXXXXXXX.js.pre-truecloud-patch \
   /usr/share/truenas/webui/main.XXXXXXXX.js
```

Refresh your browser. The UI will return to normal (Storj-only until the
patch re-runs at next reboot, or you run
`bash /mnt/tank/truenas-truecloud-patch/patch/apply.sh` manually).

---

### Backend verify shows FAIL

```bash
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify
```

`verify` reports one line per module:

| Label | Meaning |
|---|---|
| `[OK  ]` | Module is active and applied. |
| `[SKIP]` | Module is inactive — either TrueNAS now does it natively, or it is opt-in and switched off. **Not a failure.** `nested_snapshots` shows SKIP on a default install. |
| `[FAIL]` | Module is needed but did not apply. |

If a module shows `[FAIL]`:

1. **Check the apply log** for errors during the last boot:
   ```bash
   tail -40 /mnt/tank/truenas-truecloud-patch/apply.log
   ```
2. **Check middlewared's own log** for Python tracebacks:
   ```bash
   grep -i "truecloud\|traceback\|error" /var/log/middlewared.log 2>/dev/null | tail -30
   journalctl -u middlewared -n 50
   ```
3. **A FAIL is non-fatal.** middlewared runs normally and the other module is
   unaffected; the failed one is simply inactive. Existing backups are not at
   risk.
4. **If the detail says the module doesn't exist**, a TrueNAS update renamed
   or restructured the internal API.
   [Open an issue](https://github.com/sudolulo/truenas-truecloud-patch/issues)
   with your TrueNAS version number and the full verify output.

---

## Troubleshooting

**Backups fail with `NotImplementedError` after a reboot**

The traceback ends in `rclone/base.py` → `raise NotImplementedError` and
contains no `_tc_` frames: the running middlewared is executing stock code.
Either the deferred restart never fired, the patch never landed on disk this
boot, or it landed and was then torn off before the restart. Diagnose in this
order:

```bash
# Did apply.sh run this boot, at which version, and did it schedule the restart?
tail -40 /mnt/tank/truenas-truecloud-patch/apply.log

# Full check — compares the running process against the patch timestamp
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify

# What the deferred restart did -- re-apply, restart, and what it verified.
# apply.log is the durable record; journald retention on a busy box is often
# shorter than the gap between reboots, so the journal may have nothing left.
grep wait_restart /mnt/tank/truenas-truecloud-patch/apply.log | tail -20
journalctl -u truecloud-mw-restart.service --no-pager | tail -20

# Did something remount /usr and detach the patch overlay?
systemd-sysext status
findmnt -o TARGET,SOURCE /usr/lib/python3/dist-packages
```

`systemctl status truecloud-mw-restart.service` reporting *"could not be
found"* is **normal** — the unit is transient and is collected once it exits.
It is not evidence that the restart was skipped.

- `verify` reports the process started **before** the patch → the restart
  didn't happen. `systemctl restart middlewared` fixes it immediately; the
  journal output above tells you why it was missed.
- `apply.log` shows the kill switch is active → `rm .../disabled`, then
  `bash install.sh`.
- `apply.log` has no entry for this boot → the hook didn't run; re-run
  `bash install.sh` to re-register it.
- `apply.log` header shows `[v0.0.3]` or older → update:
  `git pull && bash install.sh` (v0.0.4 fixed patches not loading after
  reboot).
- `apply.log` says the patch applied, but `findmnt` shows no `truecloud-mw`
  overlay on the dist-packages path → something remounted `/usr` after our
  PREINIT hook and detached it. `systemd-sysext status` names the culprit if it
  is a sysext (the `SINCE` column will sit a few seconds *after* the `apply.log`
  timestamp). Releases from 2026-08-26 on re-apply and verify immediately before
  the restart, so this should self-heal; if you are seeing it, update first.

**TrueNAS raises "truecloud-patch is installed but NOT loaded"**

The definitive symptom, and it does not depend on a backup failing first: the
running middlewared has stock cloud_backup modules even though the patch is
installed and its providers module is meant to be active. `bash install.sh`
re-applies and restarts. The alert clears within the hour. It is silent when the
kill switch is set or the providers module has been retired as native, and it is
deliberately not muted by `update_alerts_disabled` — that silences release
notifications, not a broken backup path.

**Apply log** (check after each reboot or install):
```bash
cat /mnt/tank/truenas-truecloud-patch/apply.log
```

**Verify backend patch is loaded** (while middlewared is running):
```bash
python3 /mnt/tank/truenas-truecloud-patch/patch/create_task.py verify
```
Reads `hook_status.json` written by `apply.sh` at boot **and** checks that the
running middlewared process started *after* the patches were applied — an
on-disk patch that middlewared has not loaded yet is reported as FAIL with
instructions. Does not require `--host` or `--api-key`.

**Middlewared log:**
```bash
grep truecloud-patch /var/log/middlewared.log 2>/dev/null | tail -20
journalctl -u middlewared -n 100 2>/dev/null | grep truecloud-patch
```

**Verify the UI patch** (should print your TrueNAS version):
```bash
grep -c 'STORJ_IX.*S3.*B2' \
    $(find /usr/share/truenas -name '*.js' 2>/dev/null) 2>/dev/null \
    | grep -v ':0'
```

**`create_task.py` — "midclt not found" or permission errors**
`create_task.py` now talks to the local middleware via `midclt`, so run it **on
the TrueNAS host** (not remotely) as a user with middleware access (root). There
is no HTTPS/API-key call anymore, so there is no TLS certificate to configure.
