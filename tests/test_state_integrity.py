#!/usr/bin/env python3
"""
Test Suite for State Integrity & Security Contracts (IPSF-1.2)
Verifies:
1. ZF-026 Allowed Roots Security Contract (Fail-Closed, Traversal, UNC, Junction, Overly Broad Roots).
2. Lineage and State Integrity (consumed_at UTC ISO-8601, consumed_by tracking, immutability guarantees).
3. Atomic Metadata Writing & Fault Tolerance.
4. URL Parsing & Encoded Characters (# and ?) Protection.
5. read_handoff_summary Read-Only & Zero-Pending Protection.
6. Legacy Root Layout Compatibility.
"""

import gc
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples" / "mcp-integration"))

import contextfork_mcp


class TestStateIntegrityRemediations(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="cf_test_state_")).resolve()
        self.snapshots_dir = self.test_dir / ".contextfork" / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.db_file.name).resolve()
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

        os.environ["CONTEXTFORK_DB_PATH"] = str(self.db_path)
        os.environ["CONTEXTFORK_HOST"] = "antigravity"

        self.orig_allowed = contextfork_mcp.ALLOWED_ROOTS
        contextfork_mcp.ALLOWED_ROOTS = tuple(list(self.orig_allowed) + [self.test_dir])

        subprocess.run(["git", "init"], cwd=str(self.test_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(self.test_dir), check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=str(self.test_dir), check=True)
        (self.test_dir / "dummy.txt").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(self.test_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.test_dir), check=True)

    def tearDown(self):
        os.environ.pop("CONTEXTFORK_DB_PATH", None)
        os.environ.pop("CONTEXTFORK_HOST", None)
        os.environ.pop("CONTEXTFORK_ALLOWED_ROOTS", None)
        contextfork_mcp.ALLOWED_ROOTS = self.orig_allowed
        gc.collect()
        if hasattr(self, "db_path") and self.db_path.is_file():
            try:
                self.db_path.unlink(missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_snapshot(self, snap_id: str, consumed: bool = False, fork_ts: str = "2026-09-28T09:00:00Z"):
        s_dir = self.snapshots_dir / snap_id
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "handoff_summary.md").write_text(f"# Summary for {snap_id}\n1. Active Goal\nGoal {snap_id}", encoding="utf-8")
        meta_data = {
            "manifest_version": "1.2",
            "fork_timestamp": fork_ts,
            "consumed": consumed,
            "consumed_at": "2026-09-28T09:30:00+00:00" if consumed else None,
            "consumed_by": "prior-session-999" if consumed else None,
            "git_verification": {
                "head_commit": "1234567890abcdef",
                "branch": "main"
            },
            "lineage": {
                "parent_title": "Test Parent",
                "child_session_title": f"Test-{snap_id}"
            }
        }
        (s_dir / "session_metadata.json").write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

    # =========================================================================
    # ZF-026 ALLOWED ROOTS SECURITY CONTRACT TESTS
    # =========================================================================

    def test_allowed_roots_relative_path_rejected(self):
        """ZF-026 Rule 3: Relative path rejected (Fail-closed)."""
        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=r"sub\relative\path",
            default_root=self.test_dir
        )
        self.assertIn("Relative path rejected", warn)
        self.assertEqual(roots, (self.test_dir,))

    def test_allowed_roots_dot_dot_traversal_rejected(self):
        """ZF-026 Rule 4: '..' traversal component rejected (Fail-closed)."""
        bad_path = str(self.test_dir) + r"\..\escape"
        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=bad_path,
            default_root=self.test_dir
        )
        self.assertIn("Traversal ('..') component rejected", warn)
        self.assertEqual(roots, (self.test_dir,))

    def test_allowed_roots_unc_path_rejected(self):
        """ZF-026 Rule 5: UNC / network paths rejected for NTLM leak protection."""
        for unc in [r"\\server\share\proj", r"//server/share/proj", r"\\?\C:\secret"]:
            roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
                env_str=unc,
                default_root=self.test_dir
            )
            self.assertIn("UNC / network path or device path rejected", warn)
            self.assertEqual(roots, (self.test_dir,))

    def test_allowed_roots_drive_root_rejected(self):
        """ZF-026 Rule 7: Overly broad drive roots rejected."""
        drive = Path(self.test_dir).drive + "\\"
        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=drive,
            default_root=self.test_dir
        )
        self.assertIn("Overly broad drive root rejected", warn)
        self.assertEqual(roots, (self.test_dir,))

    def test_allowed_roots_home_dir_rejected(self):
        """ZF-026 Rule 7: User home directory and ancestors rejected."""
        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=str(Path.home()),
            default_root=self.test_dir
        )
        self.assertIn("Overly broad user home directory", warn)
        self.assertEqual(roots, (self.test_dir,))

    def test_allowed_roots_semicolon_parsing_valid(self):
        """ZF-026 Rule 1 & 2: Multiple valid roots parsed with fixed ';' delimiter."""
        dir1 = self.test_dir / "valid1"
        dir2 = self.test_dir / "valid2"
        dir1.mkdir()
        dir2.mkdir()

        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=f"{dir1};{dir2}; {dir1} ;",
            default_root=self.test_dir
        )
        self.assertIsNone(warn)
        self.assertIn(dir1.resolve(), roots)
        self.assertIn(dir2.resolve(), roots)
        self.assertIn(self.test_dir.resolve(), roots)

    def test_allowed_roots_mixed_valid_invalid_fail_closed(self):
        """Fail-Closed: When mixed valid and invalid roots are provided, all roots are rejected."""
        dir1 = self.test_dir / "valid1"
        dir1.mkdir()
        bad = r"relative\dir"

        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=f"{dir1};{bad}",
            default_root=self.test_dir
        )
        self.assertIsNotNone(warn)
        self.assertIn("Relative path rejected", warn)
        # dir1 must NOT be in roots due to fail-closed
        self.assertNotIn(dir1.resolve(), roots)
        self.assertEqual(roots, (self.test_dir.resolve(),))

    def test_allowed_roots_fail_closed_status_warning(self):
        """ZF-026 Rule 9: Visible warning displayed in get_context_status output when invalid roots configured."""
        orig_warn = contextfork_mcp.ALLOWED_ROOTS_WARNING
        try:
            contextfork_mcp.ALLOWED_ROOTS_WARNING = "⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: Test Warning"
            status = contextfork_mcp.get_context_status(workspace_path=str(self.test_dir))
            self.assertIn("⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected", status)
        finally:
            contextfork_mcp.ALLOWED_ROOTS_WARNING = orig_warn

    def test_allowed_roots_junction_rejected(self):
        """ZF-026 Rule 8: Junction root rejected (Fail-Closed)."""
        if sys.platform != "win32":
            self.skipTest("Junction test is Windows-specific")
        import _winapi
        target_src = self.test_dir / "real_target"
        target_src.mkdir()
        junction_dir = self.test_dir / "junction_link"
        try:
            _winapi.CreateJunction(str(target_src), str(junction_dir))
        except Exception as e:
            self.skipTest(f"Cannot create junction: {e}")

        roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
            env_str=str(junction_dir),
            default_root=self.test_dir
        )
        self.assertIsNotNone(warn)
        self.assertIn("Junction/symlink root rejected", warn)
        self.assertEqual(roots, (self.test_dir.resolve(),))

    def test_allowed_roots_default_when_not_git_repo(self):
        """Rule 11: If default_root is not a git repository, returns empty list and warning."""
        non_git = Path(tempfile.mkdtemp(prefix="cf_non_git_")).resolve()
        try:
            roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(
                env_str="",
                default_root=non_git
            )
            self.assertEqual(roots, ())
            self.assertIsNotNone(warn)
            self.assertIn("not a Git repository", warn)
        finally:
            shutil.rmtree(non_git, ignore_errors=True)

    def test_allowed_roots_no_hardcoded_drive_literals(self):
        """Verifies that no machine-specific hardcoded drive paths exist in contextfork_mcp.py and deploy_skill.py."""
        mcp_source = (REPO_ROOT / "examples" / "mcp-integration" / "contextfork_mcp.py").read_text(encoding="utf-8")
        drive_literals_mcp = re.findall(r'["\'][A-Za-z]:[/\\][^"\']*["\']', mcp_source)
        self.assertEqual(drive_literals_mcp, [], f"Hardcoded drive paths found in MCP server: {drive_literals_mcp}")

        deploy_source = (REPO_ROOT / "scripts" / "deploy_skill.py").read_text(encoding="utf-8")
        drive_literals_deploy = re.findall(r'["\'][A-Za-z]:[/\\][^"\']*["\']', deploy_source)
        self.assertEqual(drive_literals_deploy, [], f"Hardcoded drive paths found in deploy_skill.py: {drive_literals_deploy}")

    # =========================================================================
    # LINEAGE AND STATE INTEGRITY TESTS
    # =========================================================================

    def test_consumed_at_is_utc_datetime_after_fork(self):
        """consumed_at records a valid UTC ISO datetime greater than fork_timestamp."""
        fork_time_str = "2026-09-28T09:00:00+00:00"
        self._create_snapshot("260928-090000", consumed=False, fork_ts=fork_time_str)

        res = contextfork_mcp.resume_session_context(
            workspace_path=str(self.test_dir),
            package_id="260928-090000",
            session_id="session-test-utc"
        )
        self.assertIn("260928-090000", res)

        meta = json.loads((self.snapshots_dir / "260928-090000" / "session_metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["consumed"])
        consumed_at_str = meta["consumed_at"]

        # Parse datetime
        consumed_dt = datetime.fromisoformat(consumed_at_str)
        fork_dt = datetime.fromisoformat(fork_time_str)

        self.assertGreater(consumed_dt, fork_dt, "consumed_at must be strictly greater than fork_timestamp!")
        self.assertEqual(meta["consumed_by"], "session-test-utc")

    def test_consumed_by_sanitizes_invalid_session_id(self):
        """Invalid session_id is saved as null and warning is returned in output."""
        self._create_snapshot("260928-100000", consumed=False)

        invalid_sid = "bad\nsession\0id" + "x" * 200
        res = contextfork_mcp.resume_session_context(
            workspace_path=str(self.test_dir),
            package_id="260928-100000",
            session_id=invalid_sid
        )
        self.assertIn("session_id", res)
        meta = json.loads((self.snapshots_dir / "260928-100000" / "session_metadata.json").read_text(encoding="utf-8"))
        self.assertIsNone(meta["consumed_by"])

    def test_second_resume_preserves_consumed_at_and_by(self):
        """Reloading a consumed package preserves original consumed_at and consumed_by."""
        self._create_snapshot("260928-110000", consumed=True, fork_ts="2026-09-28T08:00:00+00:00")
        meta_file = self.snapshots_dir / "260928-110000" / "session_metadata.json"
        orig_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        orig_consumed_at = orig_meta["consumed_at"]
        orig_consumed_by = orig_meta["consumed_by"]

        res = contextfork_mcp.resume_session_context(
            workspace_path=str(self.test_dir),
            package_id="260928-110000",
            session_id="new-intruder-session"
        )
        self.assertIn("260928-110000", res)

        after_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(after_meta["consumed_at"], orig_consumed_at, "consumed_at must NOT be overwritten!")
        self.assertEqual(after_meta["consumed_by"], orig_consumed_by, "consumed_by must NOT be overwritten!")

    def test_atomic_write_metadata_failure_resilience(self):
        """Failure during atomic metadata write preserves previous metadata without corruption."""
        self._create_snapshot("260928-120000", consumed=False)
        meta_file = self.snapshots_dir / "260928-120000" / "session_metadata.json"
        before_text = meta_file.read_text(encoding="utf-8")

        orig_replace = os.replace

        def failing_replace(src, dst):
            raise PermissionError("Simulated file lock")

        os.replace = failing_replace
        try:
            res = contextfork_mcp.resume_session_context(
                workspace_path=str(self.test_dir),
                package_id="260928-120000",
                session_id="sess-lock-test"
            )
            self.assertIn("⚠️ Package consumed but failed to update status", res)
            after_text = meta_file.read_text(encoding="utf-8")
            self.assertEqual(before_text, after_text, "Previous metadata must not be corrupted!")
            leftover_tmps = list(meta_file.parent.glob("*.tmp"))
            self.assertEqual(leftover_tmps, [], "No leftover .tmp files should exist after atomic write attempt!")
        finally:
            os.replace = orig_replace

    # =========================================================================
    # URL PARSING & TOOL TESTS
    # =========================================================================

    def test_url_parsing_hash_in_folder_name(self):
        """Correctly parses URLs with '#' in folder names."""
        csharp_dir = self.test_dir / "C#Proje"
        csharp_dir.mkdir()
        # file:///.../C%23Proje
        raw_uri = f"file:///{self.test_dir.as_posix()}/C%23Proje"

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                ("sess-csharp", "CSharp Workspace Title", "", json.dumps([raw_uri]), 500.0)
            )
            conn.commit()
        finally:
            conn.close()

        title = contextfork_mcp._resolve_conversation_title(csharp_dir, session_id="")
        self.assertEqual(title, "CSharp Workspace Title")

    def test_read_handoff_summary_readonly_and_zero_pending(self):
        """read_handoff_summary does not mutate metadata and respects zero-pending safety."""
        self._create_snapshot("260928-130000", consumed=True)
        meta_file = self.snapshots_dir / "260928-130000" / "session_metadata.json"
        before_hash = meta_file.read_text(encoding="utf-8")

        # 0 pending packages: should return notice without dumping summary
        res = contextfork_mcp.read_handoff_summary(workspace_path=str(self.test_dir))
        self.assertIn("No pending unconsumed handoff package found", res)
        self.assertIn("260928-130000", res)

        # Metadata must not be changed
        after_hash = meta_file.read_text(encoding="utf-8")
        self.assertEqual(before_hash, after_hash)

        # Specific package inspection succeeds
        res_specific = contextfork_mcp.read_handoff_summary(workspace_path=str(self.test_dir), package_id="260928-130000")
        self.assertIn("Active Goal", res_specific)

    # =========================================================================
    # LEGACY ROOT LAYOUT COMPATIBILITY
    # =========================================================================

    def test_old_root_layout_consumed_shield(self):
        """Zero-pending guard applies when legacy root has consumed: true."""
        old_test_dir = Path(tempfile.mkdtemp(prefix="cf_old_root_")).resolve()
        try:
            contextfork_mcp.ALLOWED_ROOTS = tuple(list(self.orig_allowed) + [old_test_dir])
            subprocess.run(["git", "init"], cwd=str(old_test_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(old_test_dir), check=True)
            subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=str(old_test_dir), check=True)
            (old_test_dir / "dummy.txt").write_text("dummy", encoding="utf-8")
            subprocess.run(["git", "add", "dummy.txt"], cwd=str(old_test_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(old_test_dir), check=True)

            cf_dir = old_test_dir / ".contextfork"
            cf_dir.mkdir()
            (cf_dir / "handoff_summary.md").write_text("# Legacy Root Summary", encoding="utf-8")
            (cf_dir / "session_metadata.json").write_text(json.dumps({"consumed": True}), encoding="utf-8")

            res = contextfork_mcp.resume_session_context(workspace_path=str(old_test_dir))
            self.assertIn("No pending unconsumed packages found in archive", res)
        finally:
            shutil.rmtree(old_test_dir, ignore_errors=True)

    def test_old_root_layout_missing_metadata_warning(self):
        """Loads legacy root without metadata but does not create new metadata file."""
        old_test_dir = Path(tempfile.mkdtemp(prefix="cf_old_root2_")).resolve()
        try:
            contextfork_mcp.ALLOWED_ROOTS = tuple(list(self.orig_allowed) + [old_test_dir])
            subprocess.run(["git", "init"], cwd=str(old_test_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(old_test_dir), check=True)
            subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=str(old_test_dir), check=True)
            (old_test_dir / "dummy.txt").write_text("dummy", encoding="utf-8")
            subprocess.run(["git", "add", "dummy.txt"], cwd=str(old_test_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(old_test_dir), check=True)

            cf_dir = old_test_dir / ".contextfork"
            cf_dir.mkdir()
            (cf_dir / "handoff_summary.md").write_text("# Legacy Root Summary", encoding="utf-8")

            res = contextfork_mcp.resume_session_context(workspace_path=str(old_test_dir))
            self.assertIn("Legacy Root Summary", res)
            self.assertIn("Legacy root package has no metadata file", res)
            self.assertFalse((cf_dir / "session_metadata.json").exists(), "Must NOT create metadata file silently!")
        finally:
            shutil.rmtree(old_test_dir, ignore_errors=True)

    def test_empty_workspace_path_rejected_no_fallback(self):
        """Empty workspace_path must raise an error and NEVER silently fall back to ALLOWED_ROOTS[0]."""
        res_status = contextfork_mcp.get_context_status(workspace_path="")
        self.assertIn("Missing workspace_path", res_status)
        self.assertNotIn("[Target:", res_status)

        res_whitespace = contextfork_mcp.get_context_status(workspace_path="   ")
        self.assertIn("Missing workspace_path", res_whitespace)

    def test_dynamic_allowed_roots_reload_from_config(self):
        """Newly authorized project root in mcp_config.json is dynamically loaded without server restart."""
        new_proj = Path(tempfile.mkdtemp(prefix="cf_dyn_proj_")).resolve()
        cfg_file = Path(tempfile.mktemp(suffix="_mcp_config.json")).resolve()
        try:
            subprocess.run(["git", "init"], cwd=str(new_proj), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(new_proj), check=True)
            subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=str(new_proj), check=True)
            (new_proj / "readme.md").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "readme.md"], cwd=str(new_proj), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(new_proj), check=True)

            # 1. Before authorization: new_proj is outside ALLOWED_ROOTS
            denied_res = contextfork_mcp.get_context_status(workspace_path=str(new_proj))
            self.assertIn("outside ALLOWED_ROOTS", denied_res)
            self.assertIn("ACTION REQUIRED", denied_res)

            # 2. Simulate user authorizing new_proj via ask_question card -> written to mcp_config.json
            cfg_data = {
                "mcpServers": {
                    "contextfork": {
                        "env": {
                            "CONTEXTFORK_ALLOWED_ROOTS": f"{self.test_dir};{new_proj}"
                        }
                    }
                }
            }
            cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")
            os.environ["CONTEXTFORK_CONFIG_PATH"] = str(cfg_file)

            # 3. Retry immediately without restarting server -> succeeds!
            allowed_res = contextfork_mcp.get_context_status(workspace_path=str(new_proj))
            self.assertIn(f"[Target: {new_proj}]", allowed_res)
        finally:
            os.environ.pop("CONTEXTFORK_CONFIG_PATH", None)
            if cfg_file.is_file():
                cfg_file.unlink(missing_ok=True)
            shutil.rmtree(new_proj, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

