"""Run the independent PostgreSQL-backed collector on Windows or Linux."""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Non-secret ScweetConfig JSON path")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
    parser.add_argument("--no-schedule", action="store_true", help="Process queued jobs without adding scheduled jobs")
    parser.add_argument("--fixtures", help="Replay captured response directory; never connect to X")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        from scweet_mcp.settings import load_settings
        from scweet_mcp.store import TweetStore, StoreError
        from scweet_mcp.source import Source
        from scweet_mcp.worker import Worker
        from scweet_mcp.notifications import deliver_one
        config = load_settings(args.config)
        # Source failures can contain response fragments. This independent process emits safe codes.
        # Do not change library loggers when the adapter is merely imported or constructed.
        for name in ("Scweet.api_engine", "Scweet.transaction", "Scweet.manifest", "Scweet.account_session"):
            logging.getLogger(name).disabled = True
        store = TweetStore(config)
        store.check_schema()
        worker = Worker(store, Source(store, args.fixtures))
        while True:
            worked = worker.tick(schedule=not args.no_schedule)
            if not args.fixtures and config.mcp_notifications_enabled:
                deliver_one(store)
            if args.once:
                return 0
            if not worked:
                time.sleep(config.mcp_worker_poll_s)
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger("scweet_mcp.worker").error("Worker stopped. Check dependencies, database schema and configuration; no secret details are logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
