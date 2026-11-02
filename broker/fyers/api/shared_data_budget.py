"""One account-wide window for Fyers DATA calls, shared across processes.

WHY THIS EXISTS (O119 B, 2026-11-02). Every Fyers data call on this machine
passes through OpenAlgo: the trading bot's quotes, the OI collector's futures
quotes, and the sandbox's own mark-to-market polls all arrive here. Until now
nothing counted their sum. `rate_limiter.py` paced them process-wide at about
8 a second and counted nothing, while the bot kept its own per-process count
in its own file - so three callers each believed they were alone under the
limit and Fyers saw the total. That is the whole of the 429 story: 71 rate-limit
events across the seven sessions from 2026-10-22, the worst day being 29 Oct
with 25.

THE LIMITS. The Fyers DATA API on the Standard plan allows 5 requests a
second, 50 a minute and 5,000 a day, and more than three per-minute breaches
in one day can block the data API for the rest of it. The 10/s, 200/min,
100,000/day figures that used to appear in this package's docstrings are the
TRANSACTION API's - orders, orderbook, positions, funds - which is a different
bucket and is not counted here.

WHAT THIS IS, AND IS NOT. It is the account's backstop and its only honest
counter: it paces at 4 a second and 45 a minute, under the published 5 and 50,
and records every data call it let through. It is deliberately looser than the
bot's own client-side pacing (3 a second, 30 a minute) so that it never becomes
the bottleneck for a session that is already pacing itself properly - it exists
to catch what the bot cannot see.

NO DEPENDENCIES BEYOND THE STANDARD LIBRARY, on purpose: the bot's test suite
loads this file directly by path and exercises it, so the one piece of logic
that both processes depend on is covered by the suite that actually runs
(the fork has no pytest suite of its own).

FAIL OPEN, ALWAYS. A broken clock, an unreadable file, a lock that cannot be
taken: every failure path returns "no wait" and lets the call through. A data
call refused because the accounting is broken is worse than a call that risks
a 429, because the accounting is not what the money depends on.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

#: Env var naming the shared JSON file. Unset means this is switched off and
#: the limiter behaves exactly as it did before: process-wide pacing, no count.
SHARED_BUDGET_ENV = "OPENALGO_SHARED_DATA_BUDGET"

#: Under the published 5/s and 50/min. See the module docstring for why these
#: sit above the bot's own 3/s and 30/min rather than matching them.
PER_SECOND = 4.0
PER_MINUTE = 45

#: Only these are DATA API calls. Everything else in this package - orders,
#: orderbook, positions, funds, GTT - is the Transaction API and is not counted.
DATA_PREFIX = "/data/"

#: Our own window and count live under their own keys. The bot keeps `recent`
#: and `count` for its client-side pacing; mixing the two lists would make
#: every bot call appear twice and halve its real allowance.
WINDOW_KEY = "oa_recent"
COUNT_KEY = "openalgo"

STALE_LOCK_SEC = 5.0
LOCK_TIMEOUT_SEC = 2.0


def is_data_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint) and str(endpoint).startswith(DATA_PREFIX)


def shared_path() -> Path | None:
    raw = os.environ.get(SHARED_BUDGET_ENV, "").strip()
    return Path(raw) if raw else None


def _acquire(lock: Path) -> bool:
    deadline = time.monotonic() + LOCK_TIMEOUT_SEC
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SEC:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        except OSError:
            return False


def _read(path: Path, today: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict) or data.get("date") != today:
        # A new day resets everything, the bot's keys included: the bot does
        # the same on its side, and whichever process gets there first wins.
        data = {"date": today, "count": 0}
    if not isinstance(data.get(WINDOW_KEY), list):
        data[WINDOW_KEY] = []
    try:
        data[COUNT_KEY] = int(data.get(COUNT_KEY) or 0)
    except (TypeError, ValueError):
        data[COUNT_KEY] = 0
    return data


def book_one(path: Path, now: float | None = None, today: str | None = None) -> float:
    """Book one data call against the account's window.

    Returns the seconds the caller must wait BEFORE making the call. Zero means
    the call was booked and may go now; a positive number means nothing was
    booked and the caller should sleep that long and ask again.

    The lock is never held across a sleep: the pause is computed under it and
    slept outside it, so one caller waiting out a minute cannot stall another's
    read. Any failure returns 0.0 - see the module docstring on failing open.
    """
    try:
        now = time.time() if now is None else now
        today = today or dt.date.today().isoformat()
        lock = path.with_suffix(".lock")
        got = _acquire(lock)
        try:
            data = _read(path, today)
            window = [t for t in data[WINDOW_KEY] if isinstance(t, (int, float)) and now - t <= 60.0]
            pause = 0.0
            if len(window) >= PER_MINUTE:
                pause = max(0.0, 60.0 - (now - window[0])) + 0.05
            elif window and now - window[-1] < 1.0 / PER_SECOND:
                pause = 1.0 / PER_SECOND - (now - window[-1])
            if pause > 0:
                return pause
            window.append(now)
            data[WINDOW_KEY] = window
            data[COUNT_KEY] = data[COUNT_KEY] + 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
            return 0.0
        finally:
            if got:
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        return 0.0


def pace_data_call(endpoint: str | None, path: Path | None = None, sleep=time.sleep,
                   max_waits: int = 300) -> float:
    """Block until one data call fits the account's window, then book it.

    Returns the total seconds waited, so the caller can log it. A non-data
    endpoint, or no shared file configured, returns 0.0 and does nothing.
    `max_waits` bounds the loop so a pathological file can never hang a
    request thread forever.
    """
    if not is_data_endpoint(endpoint):
        return 0.0
    p = path or shared_path()
    if p is None:
        return 0.0
    waited = 0.0
    for _ in range(max_waits):
        pause = book_one(p)
        if pause <= 0:
            return waited
        sleep(pause)
        waited += pause
    return waited


def account_count(path: Path | None = None, today: str | None = None) -> int:
    """How many data calls this account has made today, as the only process
    that sees them all records it. Zero when switched off or unreadable."""
    p = path or shared_path()
    if p is None:
        return 0
    try:
        return _read(p, today or dt.date.today().isoformat())[COUNT_KEY]
    except Exception:
        return 0
