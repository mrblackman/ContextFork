#!/usr/bin/env python3
"""
Core Verification Suite for ContextFork MCP (IPSF-1.2)
Relocated from scratch/verify_plan08.py as permanent test suite.
"""

import gc
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples" / "mcp-integration"))

import contextfork_mcp


class TestMCPCore(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="cf_test_core_"))
        self.snapshots_dir = self.test_dir / ".contextfork" / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Temporary SQLite fixture for conversation_summaries
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.db_file.name)
        self.db_file.close()

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("""
                CREATE TABLE conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    preview TEXT,
                    workspace_uris TEXT,
                    last_modified_time REAL
                )
            """)
            conn.commit()
        finally:
            conn.close()

        # Set test environment overrides
        os.environ["CONTEXTFORK_DB_PATH"] = str(self.db_path)
        os.environ["CONTEXTFORK_HOST"] = "antigravity"

        # Temporarily register self.test_dir in ALLOWED_ROOTS tuple
        self.orig_allowed = contextfork_mcp.ALLOWED_ROOTS
        contextfork_mcp.ALLOWED_ROOTS = tuple(list(self.orig_allowed) + [self.test_dir.resolve()])

        # Initialize mock git repository in test_dir
        subprocess.run(["git", "init"], cwd=str(self.test_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(self.test_dir), check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=str(self.test_dir), check=True)
        (self.test_dir / "dummy.txt").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(self.test_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.test_dir), check=True)

    def tearDown(self):
        os.environ.pop("CONTEXTFORK_DB_PATH", None)
        os.environ.pop("CONTEXTFORK_HOST", None)
        contextfork_mcp.ALLOWED_ROOTS = self.orig_allowed
        gc.collect()
        if hasattr(self, "db_path") and self.db_path.is_file():
            try:
                self.db_path.unlink(missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_snapshot(self, snap_id: str, consumed: bool = False, corrupted: bool = False):
        s_dir = self.snapshots_dir / snap_id
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "handoff_summary.md").write_text(f"# Summary for {snap_id}\n1. Active Goal\nGoal {snap_id}", encoding="utf-8")

        meta_file = s_dir / "session_metadata.json"
        if corrupted:
            meta_file.write_text("{invalid_json_corrupted!", encoding="utf-8")
        else:
            meta_data = {
                "manifest_version": "1.2",
                "fork_timestamp": f"2026-09-28T{snap_id[:2]}:{snap_id[2:4]}:00Z",
                "consumed": consumed,
                "consumed_at": "2026-09-28T07:00:00Z" if consumed else None,
                "consumed_by": "old-session-123" if consumed else None,
                "git_verification": {
                    "head_commit": "1234567890abcdef",
                    "branch": "main"
                },
                "lineage": {
                    "parent_title": "Test Parent",
                    "child_session_title": f"Test-{snap_id}"
                }
            }
            meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

    def test_zero_pending_protection(self):
        """Zero-pending guard: No packages loaded and metadata untouched when 0 unconsumed packages remain."""
        self._create_snapshot("260928-100000", consumed=True)
        meta_file = self.snapshots_dir / "260928-100000" / "session_metadata.json"
        before_content = meta_file.read_text(encoding="utf-8")

        res = contextfork_mcp.resume_session_context(workspace_path=str(self.test_dir), package_id="")

        self.assertIn("No pending unconsumed packages found in archive", res)
        self.assertIn("/contextfork resume <package_id>", res)
        after_content = meta_file.read_text(encoding="utf-8")
        self.assertEqual(before_content, after_content)

    def test_unconsumed_priority(self):
        """Snapshot A (pending) vs Snapshot B (consumed, latest) -> A must be chosen!"""
        self._create_snapshot("260928-090000", consumed=False)
        self._create_snapshot("260928-110000", consumed=True)

        res = contextfork_mcp.resume_session_context(workspace_path=str(self.test_dir), package_id="")

        self.assertIn("260928-090000", res)
        meta_a = json.loads((self.snapshots_dir / "260928-090000" / "session_metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(meta_a["consumed"])

    def test_corrupted_metadata_tolerance_and_clean_protection(self):
        """Corrupted snapshot tolerance: Corrupted metadata does not crash resume, and clean never deletes unconsumed/corrupted packages."""
        self._create_snapshot("260928-080000", corrupted=True)
        self._create_snapshot("260928-120000", consumed=False)

        res = contextfork_mcp.resume_session_context(workspace_path=str(self.test_dir), package_id="")
        self.assertIn("260928-120000", res)
        self.assertIn("260928-080000", res)

        clean_res = contextfork_mcp.clean_session_context(workspace_path=str(self.test_dir), confirm=False)
        self.assertNotIn("260928-080000", clean_res)

    def test_zf024_sibling_traversal_blocked(self):
        """Subdirectory isolation: ContextFork_Backup substring cannot bleed into ContextFork."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                ("sess-backup", "Sibling Backup Title", "", json.dumps([f"file:///{self.test_dir.as_posix()}_Backup"]), 100.0)
            )
            conn.commit()
        finally:
            conn.close()

        title = contextfork_mcp._resolve_conversation_title(self.test_dir, session_id="")
        self.assertNotEqual(title, "Sibling Backup Title")
        self.assertEqual(title, self.test_dir.name)

    def test_zf024_encoded_drive_matched(self):
        """Percent-encoded drive URI resolver: Resolves file:///d%3A/... URIs cleanly."""
        encoded_uri = f"file:///{self.test_dir.as_posix()}".replace(":", "%3a").replace("D%3a", "d%3a")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                ("sess-legit", "Legit Workspace Title", "", json.dumps([encoded_uri]), 200.0)
            )
            conn.commit()
        finally:
            conn.close()

        title = contextfork_mcp._resolve_conversation_title(self.test_dir, session_id="")
        self.assertEqual(title, "Legit Workspace Title")

    def test_zf024_parent_directory_leak_blocked(self):
        """Parent workspace bleed guard: Parent session titles do not bleed into sub-repos."""
        parent_uri = f"file:///{self.test_dir.parent.as_posix()}"
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                ("sess-parent", "Parent Umbrella Title", "", json.dumps([parent_uri]), 300.0)
            )
            conn.commit()
        finally:
            conn.close()

        title = contextfork_mcp._resolve_conversation_title(self.test_dir, session_id="")
        self.assertNotEqual(title, "Parent Umbrella Title")
        self.assertEqual(title, self.test_dir.name)

    def test_session_id_short_circuit(self):
        """Short-circuit fallback: Does not bleed to recent conversations if explicit session_id given."""
        target_uri = f"file:///{self.test_dir.as_posix()}"
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                ("sess-old", "Old Previous Session", "", json.dumps([target_uri]), 10.0)
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                ("sess-new-empty", "", "", json.dumps([target_uri]), 20.0)
            )
            conn.commit()
        finally:
            conn.close()

        title = contextfork_mcp._resolve_conversation_title(self.test_dir, session_id="sess-new-empty")
        self.assertNotEqual(title, "Old Previous Session")
        self.assertEqual(title, self.test_dir.name)

    def test_host_signal_isolation(self):
        """Host signal isolation: Without host signal (Claude Desktop), SQLite is not accessed."""
        os.environ.pop("CONTEXTFORK_HOST", None)

        original_connect = sqlite3.connect
        connect_called = False

        def spy_connect(*args, **kwargs):
            nonlocal connect_called
            connect_called = True
            return original_connect(*args, **kwargs)

        sqlite3.connect = spy_connect
        try:
            title = contextfork_mcp._resolve_conversation_title(self.test_dir, session_id="")
            self.assertFalse(connect_called, "SQLite connection must NEVER be attempted without host signal!")
            self.assertEqual(title, self.test_dir.name)
        finally:
            sqlite3.connect = original_connect

    def test_canonical_repo_root_name(self):
        """Toplevel resolution: Returns repository name even when invoked from subdirectories."""
        subdir = self.test_dir / "src" / "deep"
        subdir.mkdir(parents=True, exist_ok=True)
        resolved = contextfork_mcp._validate_and_resolve_workspace(str(subdir))
        self.assertEqual(resolved.name, self.test_dir.name)

    def test_stdio_hygiene_stderr_logging(self):
        """Stdio hygiene: stdout JSON-RPC 0 byte pollution, diagnostics to stderr."""
        corrupted_db = tempfile.NamedTemporaryFile(delete=False)
        corrupted_db.write(b"not a valid sqlite file")
        corrupted_db.close()

        os.environ["CONTEXTFORK_DB_PATH"] = corrupted_db.name
        os.environ["CONTEXTFORK_HOST"] = "antigravity"

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture

        try:
            title = contextfork_mcp._resolve_conversation_title(self.test_dir, session_id="")
            self.assertEqual(title, self.test_dir.name)
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            gc.collect()
            try:
                Path(corrupted_db.name).unlink(missing_ok=True)
            except Exception:
                pass

        self.assertEqual(stdout_capture.getvalue(), "", "stdout must remain 100% clean for JSON-RPC transport!")
        self.assertIn("[ContextFork] Title resolver fallback", stderr_capture.getvalue())
        self.assertNotIn("SELECT", stderr_capture.getvalue(), "stderr must not leak raw SQL queries!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
