#!/usr/bin/env python3
"""
Test Suite for Deployment Automation (scripts/deploy_skill.py)
Verifies Deployment Security Shields in isolated temp directories:
1. Live tier profile blocked via --profile.
2. Dry-run zero mutation.
3. mcp_config.json preserves other MCP servers and creates timestamped backup.
4. Pre-deploy target integrity check halts on manual file modification.
5. Content-addressed dirty directory naming.
"""

import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "examples" / "mcp-integration"))

import deploy_skill


class TestDeploySkill(unittest.TestCase):

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="cf_deploy_test_")).resolve()
        self.mock_user_home = self.test_root / "Users" / "MockUser"
        self.mock_user_home.mkdir(parents=True, exist_ok=True)

        self.mock_config_dir = self.mock_user_home / ".gemini" / "antigravity"
        self.mock_config_dir.mkdir(parents=True, exist_ok=True)
        self.mock_config_file = self.mock_config_dir / "mcp_config.json"

        self.mock_skill_dir = self.mock_user_home / ".gemini" / "antigravity" / "skills" / "contextfork"
        self.mock_skill_file = self.mock_skill_dir / "SKILL.md"

        self.profile_cfg = {
            "user_name": "MockUser",
            "tier": "sandbox",
            "python_path": sys.executable,
            "config_path": str(self.mock_config_file),
            "skill_path": str(self.mock_skill_file),
            "allowed_roots": [str(REPO_ROOT)],
            "host": "antigravity"
        }

    def tearDown(self):
        gc.collect()
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_deploy_profile_live_tier_rejected(self):
        """Tier='live' profiles cannot be deployed directly via deploy_profile."""
        live_cfg = dict(self.profile_cfg)
        live_cfg["tier"] = "live"

        with self.assertRaises(SystemExit) as cm:
            deploy_skill.deploy_profile(
                profile_name="MockLive",
                profile_cfg=live_cfg,
                full_hash="1234567890abcdef",
                short_hash="1234567",
                is_dirty=False,
                is_promoting=False
            )
        self.assertNotEqual(cm.exception.code, 0)
        # Verify 0 files written to destination
        self.assertFalse(self.mock_skill_file.exists())
        self.assertFalse((self.mock_user_home / ".contextfork").exists())

    def test_deploy_dry_run_zero_mutation(self):
        """Dry-run mode does not mutate any bytes on target."""
        deploy_skill.deploy_profile(
            profile_name="MockUser",
            profile_cfg=self.profile_cfg,
            full_hash="1234567890abcdef",
            short_hash="1234567",
            is_dirty=False,
            dry_run=True,
            is_promoting=False
        )
        self.assertFalse(self.mock_skill_file.exists())
        self.assertFalse((self.mock_user_home / ".contextfork").exists())
        self.assertFalse(self.mock_config_file.exists())

    def test_deploy_mcp_config_preserves_other_servers(self):
        """Preserves existing servers, adds contextfork atomically, and creates .bak backup."""
        initial_config = {
            "mcpServers": {
                "other_tool": {
                    "command": "node",
                    "args": ["other.js"]
                }
            }
        }
        self.mock_config_file.write_text(json.dumps(initial_config, indent=2), encoding="utf-8")

        mock_bin_dir = self.mock_user_home / ".contextfork" / "bin" / "1234567"
        mock_bin_dir.mkdir(parents=True, exist_ok=True)
        (mock_bin_dir / "contextfork_mcp.py").write_text("# mock", encoding="utf-8")

        deploy_skill.update_mcp_config(self.mock_config_file, mock_bin_dir, self.profile_cfg, dry_run=False)

        updated_data = json.loads(self.mock_config_file.read_text(encoding="utf-8"))
        self.assertIn("other_tool", updated_data["mcpServers"])
        self.assertIn("contextfork", updated_data["mcpServers"])

        # Verify timestamped backup was created
        bak_files = list(self.mock_config_dir.glob("mcp_config.json.*.bak"))
        self.assertEqual(len(bak_files), 1)

    def test_deploy_manifest_verifies_hash_and_blocks_mutation(self):
        """Halts if target file was manually modified unless force-overwrite is specified."""
        # First successful deployment
        deploy_skill.deploy_profile(
            profile_name="MockUser",
            profile_cfg=self.profile_cfg,
            full_hash="1234567890abcdef",
            short_hash="1234567",
            is_dirty=False,
            dry_run=False,
            is_promoting=False
        )
        self.assertTrue(self.mock_skill_file.is_file())

        # Simulate manual modification on target SKILL.md
        self.mock_skill_file.write_text("# Tampered SKILL.md by developer", encoding="utf-8")

        # Second deployment should fail due to manual modifications
        with self.assertRaises(SystemExit) as cm:
            deploy_skill.deploy_profile(
                profile_name="MockUser",
                profile_cfg=self.profile_cfg,
                full_hash="1234567890abcdef",
                short_hash="1234567",
                is_dirty=False,
                dry_run=False,
                is_promoting=False,
                force_overwrite=False
            )
        self.assertNotEqual(cm.exception.code, 0)
        # Verify file still contains developer's tampered version (not overwritten!)
        self.assertEqual(self.mock_skill_file.read_text(encoding="utf-8"), "# Tampered SKILL.md by developer")

    def test_deploy_content_addressed_dirty_dir(self):
        """Dirty deployment directory is named with -dirty-<content-hash>."""
        deploy_skill.deploy_profile(
            profile_name="MockUser",
            profile_cfg=self.profile_cfg,
            full_hash="1234567890abcdef",
            short_hash="1234567",
            is_dirty=True,
            dry_run=False,
            is_promoting=False
        )
        bin_dirs = list((self.mock_user_home / ".contextfork" / "bin").glob("1234567-dirty-*"))
        self.assertEqual(len(bin_dirs), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
