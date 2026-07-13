#!/usr/bin/env python3
"""Keep ONE bug report in sync with what compat.py currently finds.

WHY THIS IS NOT JUST "POST A COMMENT"
-------------------------------------
The first version commented on every run that found a break. In one day it left
**11 identical 3,000-character comments** on the same issue. That is not a warning
system; it is a mute button with extra steps. The next real finding would have been
scrolled past, which defeats the entire point of building it.

So:

  * **The issue body is the current truth.** It is edited in place, never appended to.
  * **Comments are a changelog of CHANGES.** A run whose findings are identical to the
    last one says nothing at all -- no comment, no edit, no notification.
  * A fingerprint of the findings (broken ref/module/problem triples only) is embedded
    in the body. It deliberately ignores things that move on their own -- healthy rows,
    the hardware-verified column, TrueNAS point releases -- so `TS-25.10.4` becoming
    `TS-25.10.5` is not news, and does not wake anybody up.

  * When everything is fixed, the issue is **closed** with a comment saying so.

Works against GitHub and Gitea, which differ only in the auth header and the issue
list URL. One implementation, so the two cannot drift.

    python3 tools/compat_publish.py --api <url> --token <tok> --matrix /tmp/matrix.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from compat import (  # noqa: E402
    extract_fingerprint,
    fingerprint,
    is_broken,
    render_issue,
)

TITLE = "TrueNAS compatibility: the patch's assumptions no longer hold"


def _call(url, token, method="GET", data=None):
    req = urllib.request.Request(
        url, method=method,
        headers={
            # Gitea wants `token <t>`; GitHub accepts `Bearer <t>`. GitHub also
            # accepts `token <t>`, so one header serves both.
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        data=json.dumps(data).encode() if data else None,
    )
    with urllib.request.urlopen(req) as r:  # noqa: S310
        return json.load(r) if r.length != 0 else {}


def find_issue(api, token, title):
    """The LOWEST-numbered issue with this title, open or closed.

    Lowest, not "whichever the API returns first": two issues with the same title
    existed once (an earlier version put the ref list in the title, so the identity
    changed whenever that set changed), and an order-dependent pick would alternate
    between them -- reopening one while commenting on the other.
    """
    issues = _call(f"{api}/issues?state=all&per_page=100", token)
    mine = [
        i for i in issues
        if i.get("title") == title and "pull_request" not in i   # GitHub lists PRs here
    ]
    return min(mine, key=lambda i: i["number"]) if mine else None


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", required=True, help="…/repos/<owner>/<repo>")
    ap.add_argument("--token", required=True)
    ap.add_argument("--matrix", required=True, help="compat.py --matrix --json output")
    args = ap.parse_args(argv[1:])

    with open(args.matrix, encoding="utf-8") as fh:
        rows = json.load(fh)

    broken = [r for r in rows if any(is_broken(m) for m in r["modules"].values())]
    issue = find_issue(args.api, args.token, TITLE)

    # ── everything is healthy ────────────────────────────────────────────────
    if not broken:
        if issue and issue["state"] == "open":
            _call(f"{args.api}/issues/{issue['number']}/comments", args.token, "POST",
                  {"body": "All of the patch's assumptions hold again on every "
                           "checked TrueNAS version. Closing."})
            _call(f"{args.api}/issues/{issue['number']}", args.token, "PATCH",
                  {"state": "closed"})
            print(f"closed #{issue['number']} — nothing is broken any more")
        else:
            print("nothing broken; no open report to close")
        return 0

    body = render_issue(rows)
    want = fingerprint(rows)

    # ── nothing to file yet ──────────────────────────────────────────────────
    if issue is None:
        made = _call(f"{args.api}/issues", args.token, "POST",
                     {"title": TITLE, "body": body})
        print(f"filed #{made['number']}")
        return 0

    have = extract_fingerprint(issue.get("body") or "")
    n = issue["number"]

    # ── the findings are UNCHANGED: say nothing ──────────────────────────────
    #
    # This is the whole point. A daily "still broken, same as yesterday" comment is
    # what taught everyone to ignore the last one.
    if have == want and issue["state"] == "open":
        print(f"#{n} is already current ({want}) — staying quiet")
        return 0

    _call(f"{args.api}/issues/{n}", args.token, "PATCH", {"body": body, "state": "open"})

    if have != want:
        refs = ", ".join(f"`{r['ref']}`" for r in broken)
        note = (
            "The findings changed — the report above has been updated.\n\n"
            f"Currently broken on: {refs}."
            if have else
            "This report is now kept up to date automatically: the body above always "
            "reflects the current findings, and a comment is only added when they "
            "change."
        )
        _call(f"{args.api}/issues/{n}/comments", args.token, "POST", {"body": note})
        print(f"updated #{n}: {have} -> {want}")
    else:
        print(f"reopened #{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
