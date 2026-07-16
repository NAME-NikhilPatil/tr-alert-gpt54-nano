"""Regression coverage for SQLite startup locking in Azure Container Apps."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db_manager


class DbManagerLockingTests(unittest.TestCase):
    def test_init_retries_a_transient_database_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "seen.db"
            real_connection = db_manager._db_connection
            attempts = 0

            def transient_lock(path: Path, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                return real_connection(path, **kwargs)

            env = {
                "SQLITE_INIT_RETRY_SECONDS": "1",
                "SQLITE_INIT_RETRY_DELAY_SECONDS": "0.01",
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(db_manager, "_db_connection", side_effect=transient_lock):
                    db_manager.init_seen_db(db_path)

            self.assertGreaterEqual(attempts, 2)
            connection = sqlite3.connect(db_path)
            try:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='seen_pdfs'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(table, ("seen_pdfs",))

    def test_schema_initialization_is_cached_per_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "seen.db"
            real_connection = db_manager._db_connection
            calls = 0

            def counted_connection(path: Path, **kwargs):
                nonlocal calls
                calls += 1
                return real_connection(path, **kwargs)

            with patch.object(db_manager, "_db_connection", side_effect=counted_connection):
                db_manager.init_seen_db(db_path)
                db_manager.init_seen_db(db_path)

            self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
