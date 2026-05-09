"""Mark past notifications as good (kept) or bad (false positive).

This is how the user retroactively builds a golden set without sitting down to
make one. Run it whenever a phone push turns out (un)worthwhile.

Usage:
  uv run python -m radar.label <notification_id> good|bad [--reason "..."]
  uv run python -m radar.label list [--track confirmed|speculative] [--undismissed]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from radar.db import RadarDB

DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")


async def label_one(db: RadarDB, notif_id: int, verdict: str, reason: str | None) -> None:
    correct = 1 if verdict == "good" else 0
    cur = await db.conn.execute(
        "UPDATE notifications SET dismissed_correct=?, dismissed_reason=? WHERE id=?",
        (correct, reason, notif_id),
    )
    await db.conn.commit()
    if cur.rowcount == 0:
        print(f"no notification with id={notif_id}", file=sys.stderr)
        sys.exit(1)
    print(f"notif #{notif_id} → {verdict}" + (f" — {reason}" if reason else ""))


async def list_notifs(db: RadarDB, track: str | None, undismissed_only: bool) -> None:
    sql = (
        "SELECT n.id, n.track, n.sent_at, n.dismissed_correct, n.dismissed_reason, "
        "       i.html_url, i.title, e.scope_bucket, e.difficulty, e.why "
        "  FROM notifications n "
        "  JOIN issues i ON i.id = n.issue_id "
        "  JOIN issue_evaluations e ON e.id = n.evaluation_id "
        " WHERE 1=1"
    )
    params: list = []
    if track:
        sql += " AND n.track = ?"
        params.append(track)
    if undismissed_only:
        sql += " AND n.dismissed_correct IS NULL"
    sql += " ORDER BY n.sent_at DESC LIMIT 50"

    async with db.conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    if not rows:
        print("(no notifications)")
        return
    for r in rows:
        verdict = (
            "?" if r["dismissed_correct"] is None
            else ("good" if r["dismissed_correct"] else "bad")
        )
        print(
            f"#{r['id']:>4} [{r['track']:<11}] D{r['difficulty']} {r['scope_bucket']:<13} "
            f"{verdict:<4} {r['title'][:70]}"
        )
        print(f"      {r['html_url']}")
        if r["why"]:
            print(f"      why: {r['why'][:100]}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="label notifications good/bad")
    sub = p.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("set", help="(default) mark a notification good or bad")
    one.add_argument("notif_id", type=int)
    one.add_argument("verdict", choices=["good", "bad"])
    one.add_argument("--reason", default=None)

    lst = sub.add_parser("list", help="show recent notifications")
    lst.add_argument("--track", choices=["confirmed", "speculative"], default=None)
    lst.add_argument("--undismissed", action="store_true",
                     help="only show notifications with no verdict yet")

    return p.parse_args()


def parse_args_with_default() -> argparse.Namespace:
    """Allow `radar.label 7 bad` and `radar.label list` as well as `radar.label set 7 bad`."""
    argv = sys.argv[1:]
    if argv and argv[0] not in {"set", "list", "-h", "--help"}:
        # numeric first arg → implicit `set`
        try:
            int(argv[0])
            sys.argv.insert(1, "set")
        except ValueError:
            pass
    return parse_args()


async def main() -> None:
    args = parse_args_with_default()
    async with RadarDB(DEFAULT_DB_PATH) as db:
        if args.cmd == "set":
            await label_one(db, args.notif_id, args.verdict, args.reason)
        elif args.cmd == "list":
            await list_notifs(db, args.track, args.undismissed)


if __name__ == "__main__":
    asyncio.run(main())
