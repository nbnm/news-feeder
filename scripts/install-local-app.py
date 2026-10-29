#!/usr/bin/env python3
"""Build/install the local macOS app and safely import the development archive."""

import argparse
import os
import plistlib
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "app.newsfeeder.mac"


def migrate_database(source, destination):
    if not source.exists() or destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(destination.name + ".import-" + uuid4().hex)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            if not original.execute("SELECT name FROM sqlite_master WHERE name='articles'").fetchone():
                return False
            with closing(sqlite3.connect(temporary)) as copied:
                original.backup(copied)
                copied.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Archive integrity check failed")
        temporary.chmod(0o600)
        temporary.replace(destination)
        return True
    finally:
        for suffix in ["", "-wal", "-shm"]:
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def install(applications_dir, support_dir):
    source = ROOT / "dist/News Feeder.app"
    helper = source / "Contents/Resources/Backend/NewsFeederService"
    if not helper.is_file():
        raise RuntimeError("Build the bundled app with scripts/build-local-app.sh first")
    applications_dir.mkdir(parents=True, exist_ok=True)
    support_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    support_dir.chmod(0o700)
    target = applications_dir / "News Feeder.app"
    commands = subprocess.check_output(["ps", "-ax", "-o", "command="], text=True).splitlines()
    for app in [source, target]:
        executable = str(app / "Contents/MacOS/NewsFeeder")
        helper_executable = str(app / "Contents/Resources/Backend/NewsFeederService")
        if any(
            command.strip() == executable or command.strip().startswith(helper_executable + " ")
            for command in commands
        ):
            raise RuntimeError(
                "Quit News Feeder and let any scheduled digest finish before installing an update"
            )
    if target.exists():
        with (target / "Contents/Info.plist").open("rb") as stream:
            if plistlib.load(stream).get("CFBundleIdentifier") != BUNDLE_ID:
                raise RuntimeError("Another application already uses the News Feeder name")

    service_dir = support_dir / "service"
    service_dir.mkdir(exist_ok=True, mode=0o700)
    migrated = migrate_database(ROOT / "backend/data/newsfeeder.db", service_dir / "newsfeeder.db")
    for cache in (ROOT / "dist/live-cache").glob("*.sqlite"):
        migrate_database(cache, support_dir / cache.name)
    environment = ROOT / "backend/.env"
    if environment.exists() and not (service_dir / ".env").exists():
        shutil.copy2(environment, service_dir / ".env")
        (service_dir / ".env").chmod(0o600)

    staging = applications_dir / (".News Feeder.install-" + uuid4().hex + ".app")
    previous = None
    try:
        subprocess.run(["ditto", str(source), str(staging)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staging)], check=True)
        if target.exists():
            backup = support_dir / "backups"
            backup.mkdir(exist_ok=True, mode=0o700)
            previous = backup / ("News Feeder-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".app")
            shutil.move(str(target), str(previous))
        staging.replace(target)
    except BaseException:
        if previous and previous.exists() and not target.exists():
            shutil.move(str(previous), str(target))
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print("Installed:", target)
    print("Data:", support_dir)
    print(
        "Imported existing service archive:" if migrated else "Preserved existing service archive:",
        service_dir / "newsfeeder.db",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_apps = (
        Path("/Applications") if os.access("/Applications", os.W_OK) else Path.home() / "Applications"
    )
    parser.add_argument("--applications-dir", type=Path, default=default_apps)
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    if not args.no_build:
        subprocess.run(["bash", str(ROOT / "scripts/build-local-app.sh")], cwd=ROOT, check=True)
    install(
        args.applications_dir.expanduser().resolve(), Path.home() / "Library/Application Support/NewsFeeder"
    )


if __name__ == "__main__":
    main()
