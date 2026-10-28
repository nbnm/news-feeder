#!/usr/bin/env python3
"""Launch the real feed with a local service; quit the app to stop the service."""

import argparse
from datetime import datetime
import fcntl
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "dist"
output.mkdir(exist_ok=True)
port = os.environ.get("NEWS_FEEDER_PORT", "8000")
url = f"http://127.0.0.1:{port}"
environment = {
    **os.environ,
    "DEMO_MODE": "false",
    "NEWS_FEEDER_BACKEND_URL": url,
    "NEWS_FEEDER_DATA_DIR": str(output / "live-cache"),
}
children = []


def event(message):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def stop(signum, _):
    event(f"Received {signal.Signals(signum).name}; stopping the app and local service.")
    raise SystemExit(0)


def cleanup():
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        event(f"Child PID {child.pid} exited with code {child.returncode}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--detach", action="store_true",
        help="Run independently of the terminal or chat session; quit the app to stop.",
    )
    args = parser.parse_args()
    if args.detach:
        with (output / "launcher.log").open("a") as log:
            launcher = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())], cwd=root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
        print(f"Started detached launcher PID {launcher.pid}. Logs: {output / 'launcher.log'}")
        return

    # Only one local launcher may own this archive and collection service.
    with (output / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("News Feeder is already running.")
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        event(f"Starting launcher PID {os.getpid()}.")
        try:
            subprocess.run([str(root / "scripts/build-macos.sh")], check=True)
            with (output / "server.log").open("a") as log, (output / "app.log").open("a") as app_log:
                server = subprocess.Popen(
                    [str(root / "backend/.venv/bin/uvicorn"), "app.main:app",
                     "--host", "127.0.0.1", "--port", port, "--no-access-log"],
                    cwd=root / "backend", env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                )
                children.append(server)
                for _ in range(100):
                    if server.poll() is not None:
                        raise SystemExit("Backend failed. See dist/server.log; the port may already be in use.")
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise SystemExit("Backend startup timed out. See dist/server.log.")
                app = subprocess.Popen(
                    [str(output / "News Feeder.app/Contents/MacOS/NewsFeeder")],
                    env=environment, stdin=subprocess.DEVNULL,
                    stdout=app_log, stderr=app_log,
                )
                children.append(app)
                event(f"Live feed open. App PID {app.pid}; backend PID {server.pid}: {url}.")
                event("Quit the app to stop the local service.")
                code = app.wait()
                event(f"App exited with code {code}." if code >= 0 else
                      f"App ended by signal {signal.Signals(-code).name}.")
        finally:
            cleanup()
            event("Launcher stopped.")


if __name__ == "__main__":
    main()
