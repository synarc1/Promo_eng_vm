#!/usr/bin/env python3
"""Payout poller, step 1: LOOK at the payout queue in Supabase. It changes nothing.

Every query runs in a READ ONLY transaction, so a mistake in this script cannot claim, update
or delete a payout. The real protection is the database login: give it SELECT on public.payout
and nothing else. It reports how many payouts are pending and how long the oldest has waited.
Later steps add claiming and the partner call.

Run:
  python3 payout_poller.py            # keeps polling every 5 seconds
  python3 payout_poller.py --once     # one check, then exit (exit code 1 if it can't connect)

Environment (first one found is used):
  PAYOUT_DATABASE_URL, CODEGEN_DATABASE_URL, DATABASE_URL
Use the Supabase session pooler string (port 5432). Plain Python, no web server, no open ports.
Nothing personal is printed: only a short payout id, the amount and the age.
"""
import argparse
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg import errors as pgerr
except ImportError:
    sys.exit("Install the driver first:  pip install 'psycopg[binary]'")

stop = False


def _stop(signum, frame):
    global stop
    stop = True
    log("stopping")


def log(text):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {text}", flush=True)


def connect(dsn):
    conn = psycopg.connect(dsn, connect_timeout=10)
    conn.read_only = True            # every transaction starts as READ ONLY; the database refuses writes
    return conn


def ago(seconds):
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def sleep_until(seconds):
    end = time.monotonic() + seconds
    while not stop and time.monotonic() < end:
        time.sleep(0.25)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    ap.add_argument("--show", type=int, default=10, help="how many pending payouts to list")
    ap.add_argument("--heartbeat", type=int, default=12, help="print a status line every N quiet polls")
    ap.add_argument("--stale-minutes", type=float, default=5.0, help="warn when the oldest pending payout is older")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    dsn = next((os.environ[k] for k in ("PAYOUT_DATABASE_URL", "CODEGEN_DATABASE_URL", "DATABASE_URL") if os.environ.get(k)), None)
    if not dsn:
        sys.exit("Set PAYOUT_DATABASE_URL (or CODEGEN_DATABASE_URL / DATABASE_URL) first.")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    conn, failures, polls, last_ids, last_warn = None, 0, 0, None, 0.0
    log(f"payout poller started (read-only, every {a.interval:g}s)")

    while not stop:
        try:
            if conn is None or conn.closed:
                conn = connect(dsn)
                if failures:
                    log("reconnected")
            count, oldest = conn.execute(
                """select count(*), coalesce(extract(epoch from now() - min(created_at)), 0)
                     from public.payout where status = 'pending'""").fetchone()
            rows = conn.execute(
                """select id, amount, currency, extract(epoch from now() - created_at)
                     from public.payout where status = 'pending'
                    order by created_at limit %s""", (a.show,)).fetchall() if count else []
            conn.rollback()                  # end the read-only transaction between polls
            failures = 0
            polls += 1

            ids = tuple(r[0] for r in rows)
            stale = bool(count) and oldest > a.stale_minutes * 60
            warn_due = stale and time.monotonic() - last_warn >= 60    # repeat the warning once a minute
            if ids != last_ids or warn_due or polls % a.heartbeat == 0:
                line = f"pending: {count}" + (f" (oldest {ago(oldest)})" if count else "")
                log(line + ("  WARNING: waiting longer than expected" if stale else ""))
                if warn_due:
                    last_warn = time.monotonic()
                for pid, amount, currency, age in rows:
                    print(f"            {str(pid)[:8]}  {amount:,.2f} {currency}  waiting {ago(age)}")
                if count > len(rows):
                    print(f"            ... and {count - len(rows)} more")
            last_ids = ids

            if a.once:
                return
            sleep_until(a.interval + random.random())

        except (pgerr.UndefinedTable, pgerr.InsufficientPrivilege) as e:
            sys.exit(f"Cannot read public.payout: {str(e).splitlines()[0]}\n"
                     "Check the table exists and this database login may SELECT from it.")
        except psycopg.Error as e:
            failures += 1
            if conn is not None:
                conn.close()
            conn = None
            wait = min(60.0, a.interval * 2 ** failures)
            log(f"database error: {str(e).splitlines()[0]}  (retrying in {wait:.0f}s)")
            if a.once:
                sys.exit(1)
            sleep_until(wait)


if __name__ == "__main__":
    main()
