"""Frozen, app-owned local service. Never write inside the application bundle."""

import argparse
import asyncio
import multiprocessing
import os
import signal
import threading
import time


def watch_parent(parent_pid):
    while os.getppid() == parent_pid:
        time.sleep(2)
    os.kill(os.getpid(), signal.SIGTERM)


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--daily-digest", action="store_true")
    args = parser.parse_args()
    if args.daily_digest:
        os.environ["WORKER_ENABLED"] = "false"
        from app.daily_runner import run_scheduled

        try:
            result = asyncio.run(run_scheduled(args.port))
            print("Scheduled digest:", result["state"], result.get("reason") or "")
        except Exception as error:
            # Never log exception text: HTTP client errors can include bot URLs.
            print("Scheduled digest failed:", type(error).__name__)
            raise SystemExit(1) from None
        return
    if args.parent_pid:
        threading.Thread(target=watch_parent, args=(args.parent_pid,), daemon=True).start()
    import uvicorn

    from app.daily_runner import service_lock
    from app.main import app

    try:
        with service_lock():
            uvicorn.run(
                app, host="127.0.0.1", port=args.port, loop="asyncio", http="h11", ws="none", access_log=False
            )
    except BlockingIOError:
        raise SystemExit("The local service is already starting or running") from None


if __name__ == "__main__":
    main()
