import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "local_installer", Path(__file__).resolve().parents[2] / "scripts/install-local-app.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_migration_captures_wal_and_keeps_existing_installed_data(tmp_path):
    source, destination = tmp_path / "development.sqlite", tmp_path / "installed/archive.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE articles(id TEXT PRIMARY KEY, isRead INTEGER, isSaved INTEGER)")
        connection.execute("INSERT INTO articles VALUES ('saved-story', 1, 1)")
        connection.commit()
        assert installer.migrate_database(source, destination)
        with sqlite3.connect(destination) as copied:
            assert copied.execute("SELECT * FROM articles").fetchone() == ("saved-story", 1, 1)
            copied.execute("INSERT INTO articles VALUES ('installed-story', 0, 1)")
        connection.execute("INSERT INTO articles VALUES ('later-development-story', 0, 0)")
        connection.commit()
        assert not installer.migrate_database(source, destination)
        with sqlite3.connect(destination) as copied:
            assert {row[0] for row in copied.execute("SELECT id FROM articles")} == {
                "saved-story",
                "installed-story",
            }
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(destination.parent.glob("*.import-*"))


def test_migration_skips_uninitialized_archive(tmp_path):
    source, destination = tmp_path / "empty.sqlite", tmp_path / "target.sqlite"
    with sqlite3.connect(source):
        pass
    assert not installer.migrate_database(source, destination)
    assert not destination.exists()
