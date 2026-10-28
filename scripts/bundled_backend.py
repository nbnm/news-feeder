"""Frozen, app-owned local service. Never write inside the application bundle."""

import argparse
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
    args = parser.parse_args()
    if args.parent_pid:
        threading.Thread(target=watch_parent, args=(args.parent_pid,), daemon=True).start()
    import uvicorn

    from app.main import app

    uvicorn.run(
        app, host="127.0.0.1", port=args.port, loop="asyncio", http="h11", ws="none", access_log=False
    )


if __name__ == "__main__":
    main()
