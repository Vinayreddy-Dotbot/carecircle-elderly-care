"""Standalone reminder worker. Run it as its own process (systemd, a container, a host's background worker)
and set SCHEDULER_MODE=external for the web app, so reminders don't depend on someone having the page open.

    python worker.py            # loop forever
    python worker.py --once     # one pass (for cron)

Several workers can run: only the one holding the database lease sends reminders, and another takes over
within a few minutes if it stops.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from types import FrameType

import config
import db
import logutil
import notifier

log = logging.getLogger("carecircle.worker")


def main(argv: Sequence[str] | None = None) -> int:
    """Run reminder passes until stopped (or once with --once). Returns the process exit code."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = p.parse_args(argv)
    logutil.setup()
    errors, warnings = config.validate()
    for w in warnings:
        log.warning(w)
    if errors and config.is_production():
        for e in errors:
            log.error(e)
        return 2
    db.init_db()
    if args.once:
        ran = notifier.tick()
        log.info("single pass", extra={"ran": ran})
        return 0

    stop = threading.Event()

    def handle(sig: int, frame: FrameType | None) -> None:
        """Stop after the current pass on SIGTERM or Ctrl+C."""
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    log.info("worker started", extra={"worker": notifier.WORKER_ID, "interval": config.scheduler_interval()})
    while not stop.is_set():
        try:
            notifier.tick()
        except Exception:
            log.exception("worker pass failed")
        stop.wait(config.scheduler_interval())   # sleeps, but wakes at once when asked to stop
    db.release_lease("reminders", notifier.WORKER_ID)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
