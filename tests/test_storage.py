"""Tests for storage initialization and basic operations."""

import os
import tempfile

from conductor.storage import ConductorStorage


class TestStorageInit:
    def test_initialize_creates_db(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            assert os.path.isfile(db_path)
            assert not os.path.isfile(db_path + "-journal")

    def test_initialize_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            storage.initialize()  # second call should not raise

    def test_connect_returns_connection(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            conn = storage.connect()
            assert conn is not None
            conn.close()


class TestSchemaTables:
    def test_all_tables_present(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            conn = storage.connect()
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [r[0] for r in tables]
            assert "objectives" in table_names
            assert "objective_runs" in table_names
            assert "tasks" in table_names
            assert "agent_runs" in table_names
            assert "approvals" in table_names
            assert "events" in table_names
            assert "planner_turns" in table_names
            assert "cost_ledger" in table_names
            conn.close()

    def test_indexes_present(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            conn = storage.connect()
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
            ).fetchall()
            index_names = [r[0] for r in indexes]
            # Non-auto indexes (auto includes sqlite_autoindex_*)
            assert any("objective" in i for i in index_names)
            assert any("tasks" in i for i in index_names)
            conn.close()


class TestSchemaStructure:
    def test_objectives_columns(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            conn = storage.connect()
            info = conn.execute("PRAGMA table_info(objectives)").fetchall()
            col_names = [r[1] for r in info]
            expected = ["id", "title", "description", "status", "priority", "created_at", "updated_at", "created_by", "metadata_json"]
            for col in expected:
                assert col in col_names
            conn.close()

    def test_tasks_columns(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            storage = ConductorStorage(db_path)
            storage.initialize()
            conn = storage.connect()
            info = conn.execute("PRAGMA table_info(tasks)").fetchall()
            col_names = [r[1] for r in info]
            expected = ["id", "objective_id", "run_id", "title", "brief", "status", "task_type",
                        "depends_on_json", "required_skills_json", "dispatch_profile",
                        "approval_required", "created_at", "updated_at", "metadata_json"]
            for col in expected:
                assert col in col_names
            conn.close()