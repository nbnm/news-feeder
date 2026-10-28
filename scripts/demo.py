#!/usr/bin/env python3
"""Launch an isolated, fictional demo. Stop with Ctrl-C or quit the Mac app."""

import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "dist"
output.mkdir(exist_ok=True)
port = os.environ.get("NEWS_FEEDER_DEMO_PORT", "8765")
url = f"http://127.0.0.1:{port}"
environment = {
    **os.environ,
    "DATABASE_URL": "sqlite:///./data/demo.db",
    "DEMO_MODE": "true",
    "APP_API_KEY": "",
    "ENVIRONMENT": "development",
    "NEWS_FEEDER_BACKEND_URL": url,
    "NEWS_FEEDER_DATA_DIR": str(output / "demo-cache"),
}
children = []


def stop(*_):
    for child in reversed(children):
        child.terminate()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
subprocess.run([str(root / "scripts/build-macos.sh")], check=True)
with (output / "demo-server.log").open("w") as log:
    server = subprocess.Popen(
        [str(root / "backend/.venv/bin/uvicorn"), "app.main:app", "--host", "127.0.0.1",
         "--port", port, "--no-access-log"], cwd=root / "backend", env=environment, stdout=log, stderr=log,
    )
    children.append(server)
    for _ in range(100):
        if server.poll() is not None:
            raise SystemExit("Demo backend failed. See dist/demo-server.log; the port may already be in use.")
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        stop()
    app = subprocess.Popen([str(output / "News Feeder.app/Contents/MacOS/NewsFeeder")], env=environment)
    children.append(app)
    print(f"Demo open. Backend: {url}. All stories are fictional. Quit the app or press Ctrl-C to stop.", flush=True)
    try:
        app.wait()
    finally:
        server.terminate()
        server.wait(timeout=10)
