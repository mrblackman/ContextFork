#!/usr/bin/env python3
"""
Test Suite for Public Release Hygiene (IPSF-1.2)
Ensures zero leakage of personal paths, non-English text, unapproved directories,
or sensitive configuration files across all Git-tracked files in the repository.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Allowed top-level directories in the public repository
ALLOWED_TOP_LEVEL_DIRS = {
    ".agents",
    ".github",
    "docs",
    "examples",
    "schemas",
    "scripts",
    "tests",
}

# Forbidden configuration files that must never be tracked in git
FORBIDDEN_FILES = {
    "scripts/profiles.json",
}


class TestReleaseHygiene(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Retrieve all Git-tracked files
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=True
        )
        cls.tracked_files = [
            REPO_ROOT / p.strip()
            for p in res.stdout.splitlines()
            if p.strip()
        ]

    def test_approved_directory_structure(self):
        """Verifies that all tracked files reside within approved public top-level directories or repo root."""
        violations = []
        for fpath in self.tracked_files:
            rel = fpath.relative_to(REPO_ROOT)
            parts = rel.parts
            if len(parts) > 1:
                top_dir = parts[0]
                if top_dir not in ALLOWED_TOP_LEVEL_DIRS:
                    violations.append(f"Unapproved top-level directory: '{top_dir}' in file '{rel}'")

            rel_str = str(rel).replace("\\", "/")
            if rel_str in FORBIDDEN_FILES:
                violations.append(f"Forbidden configuration file tracked: '{rel_str}'")

        self.assertEqual(
            violations,
            [],
            "Public release directory hygiene violations detected:\n" + "\n".join(violations)
        )

    def test_no_forbidden_patterns_in_tracked_files(self):
        """Scans all tracked files for generic personal paths and non-English characters."""
        personal_path_regex = re.compile(
            r"([A-Za-z]:[/\\]Users[/\\]|WebProje)",
            re.IGNORECASE
        )

        # Non-ASCII Turkish characters using Unicode escape sequences (keeps this test file free of Turkish characters)
        turkish_regex = re.compile(
            r"[\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc]"
        )

        violations = []

        for fpath in self.tracked_files:
            # Skip this test file itself
            if fpath.name == "test_release_hygiene.py":
                continue

            # Skip binary files
            if fpath.suffix.lower() in [".png", ".jpg", ".jpeg", ".ico", ".pyc"]:
                continue

            if not fpath.is_file():
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for line_no, line in enumerate(content.splitlines(), start=1):
                # Allowed Whitelist:
                # 1. Author and citation headers
                if "Mustafa KILINC" in line or "@mrblackman" in line or "github.com/mrblackman" in line:
                    continue
                # 2. LICENSE file
                if fpath.name == "LICENSE":
                    continue

                m_path = personal_path_regex.search(line)
                if m_path:
                    violations.append(
                        f"{fpath.relative_to(REPO_ROOT)}:{line_no} -> Personal path pattern '{m_path.group(0)}': {line.strip()[:100]}"
                    )

                m_turkish = turkish_regex.search(line)
                if m_turkish:
                    violations.append(
                        f"{fpath.relative_to(REPO_ROOT)}:{line_no} -> Non-English character '{m_turkish.group(0)}': {line.strip()[:100]}"
                    )

        self.assertEqual(
            violations,
            [],
            "Public release content hygiene violations detected in tracked files:\n" + "\n".join(violations)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
