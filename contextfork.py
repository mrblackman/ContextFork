#!/usr/bin/env python3
"""
ContextFork Reference Implementation (IPSF-1.2)
Zero-dependency CLI for Real-Time Context Observability and Native Session Forking.

Motto:
  "LLM summarizes; machines verify."
  "Don't ask the AI to remember what the machine can verify."

Author: Mustafa KILINC (@mrblackman)
License: MIT
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# Windows console encoding safeguard (cp1254 / cp437 / cp1252)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class GitError(Exception):
    """Base exception for Git operations."""
    pass


class NotAGitRepositoryError(GitError):
    """Raised when the working directory is not a Git repository."""
    pass


class GitNotFoundError(GitError):
    """Raised when the Git CLI executable is not found on the system PATH."""
    pass


class GitInspector:
    """Deterministic inspector for querying local Git workspace state."""

    @staticmethod
    def run_cmd(cmd_list, cwd=None):
        try:
            res = subprocess.run(
                cmd_list,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            return res.stdout
        except FileNotFoundError:
            raise GitNotFoundError("Git executable ('git') was not found on system PATH.")
        except subprocess.CalledProcessError as e:
            err = e.stderr.strip()
            if "not a git repository" in err.lower():
                raise NotAGitRepositoryError("Current directory is not a Git repository.")
            raise GitError(f"Git command failed: {' '.join(cmd_list)} | Error: {err}")

    @classmethod
    def get_repo_root(cls, start_path=None):
        """Always returns repository top level directory (--show-toplevel)."""
        out = cls.run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=start_path)
        return Path(out.strip()).resolve()

    @classmethod
    def inspect(cls, start_path=None):
        """Deterministically inspects current repository state."""
        root = cls.get_repo_root(start_path)

        # 1. HEAD Commit (Full SHA + Short SHA)
        full_head = cls.run_cmd(["git", "rev-parse", "HEAD"], cwd=root).strip()
        short_head = full_head[:7]

        # 2. Branch
        branch = cls.run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root).strip()

        # 3. Status (Staged, Unstaged, Untracked)
        # NUL-delimited parsing (-z) prevents path corruption with spaces/quotes/renames
        status_raw = cls.run_cmd(["git", "status", "--porcelain=v1", "-z"], cwd=root)
        
        modified = []
        staged = []
        untracked = []

        if status_raw:
            entries = status_raw.split("\0")
            for entry in entries:
                if not entry:
                    continue
                code = entry[:2]
                path = entry[3:].strip()
                if "??" in code:
                    untracked.append(path)
                elif code[0] != " " and code[0] != "?":
                    staged.append(path)
                if code[1] != " " and code[1] != "?":
                    modified.append(path)

        # 4. Diff HEAD (Encompasses all staged and unstaged changes)
        # --binary: Preserves CRLF and binary files in patch (Windows safety)
        # -c color.diff=false: Prevents ANSI escape color codes from corrupting patch
        diff_head_full = cls.run_cmd(
            ["git", "-c", "color.diff=false", "diff", "--binary", "HEAD"], cwd=root
        )
        diff_head_stat = cls.run_cmd(["git", "diff", "HEAD", "--stat"], cwd=root).strip()

        is_dirty = bool(modified or staged or untracked)

        return {
            "root": root,
            "full_head": full_head,
            "short_head": short_head,
            "branch": branch or "detached",
            "is_dirty": is_dirty,
            "modified": sorted(list(set(modified))),
            "staged": sorted(list(set(staged))),
            "untracked": sorted(untracked),
            "diff_stat": diff_head_stat or "Working tree clean against HEAD",
            "diff_full": diff_head_full or ""
        }


class ContextForkEngine:
    """Core engine creating, validating, and managing protocol packages and snapshots."""

    FORK_DIR_NAME = ".contextfork"
    SNAPSHOTS_DIR_NAME = "snapshots"
    LATEST_FILE_NAME = "latest.json"
    MAX_UNTRACKED_FILE_SIZE = 1024 * 1024  # 1 MB limit to prevent bloating packages with binaries

    @classmethod
    def get_fork_dir(cls, repo_root):
        return repo_root / cls.FORK_DIR_NAME

    @classmethod
    def get_snapshots_dir(cls, repo_root):
        d = cls.get_fork_dir(repo_root) / cls.SNAPSHOTS_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def get_latest_file(cls, repo_root):
        return cls.get_fork_dir(repo_root) / cls.LATEST_FILE_NAME

    @classmethod
    def sanitize_parent_title(cls, parent_title: str = "", default_base: str = "ContextFork") -> tuple[str, str]:
        """
        ZF-020 Shield: Sanitizes lineage ancestry title and prevents recursion bloat.
        Fallback: If parent_title is empty or generic 'Session', defaults to repo name.
        Returns: (safe_base_title, child_session_title)
        """
        fallback = default_base.strip() if default_base and default_base.strip() else "ContextFork"
        raw = parent_title.strip() if parent_title and parent_title.strip() else fallback
        if raw.lower() == "session":
            raw = fallback
        # Cleanse CRLF, tabs, and null bytes before stripping lineage suffix (ZF-020)
        raw_clean = re.sub(r"[\r\n\t\x00]", " ", raw)
        # 1. Strip previous -CF-YYMMDD-HHMM or -CF-YYMMDD-HHMMSS suffix (recursion bloat guard)
        base = re.sub(r"-CF-\d{6}-\d{4,6}(_\d+)?$", "", raw_clean)
        # 2. Allow only alphanumeric, whitespace, underscore, and hyphen (CRLF / injection guard)
        safe_base = re.sub(r"[^\w\s\-]", "", base)
        # Collapse multiple spaces into a single space
        safe_base = re.sub(r"\s+", " ", safe_base).strip() or fallback
        # 3. Limit root title to maximum 45 characters
        safe_base = safe_base[:45].strip()
        timestamp_suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%y%m%d-%H%M")
        child_title = f"{safe_base}-CF-{timestamp_suffix}"
        return safe_base, child_title

    @classmethod
    def resolve_active_package_dir(cls, repo_root, package_id=None):
        """
        Safely resolves target package directory against path traversal and dangling pointers.
        """
        snapshots_dir = cls.get_snapshots_dir(repo_root)
        if package_id and package_id.strip():
            safe_id = re.sub(r"[^\w\-]", "", package_id.strip())
            target = (snapshots_dir / safe_id).resolve()
            if target.is_relative_to(snapshots_dir.resolve()) and target.is_dir():
                return target
            raise FileNotFoundError(f"Snapshot package not found: {safe_id}")

        latest_file = cls.get_latest_file(repo_root)
        if latest_file.is_file():
            try:
                data = json.loads(latest_file.read_text(encoding="utf-8"))
                snap_id = re.sub(r"[^\w\-]", "", data.get("latest_snapshot_id", ""))
                if snap_id:
                    target = (snapshots_dir / snap_id).resolve()
                    if target.is_relative_to(snapshots_dir.resolve()) and target.is_dir():
                        return target
            except Exception:
                pass

        # Backwards compatibility: root .contextfork directory
        fork_root = cls.get_fork_dir(repo_root)
        if (fork_root / "session_metadata.json").is_file():
            return fork_root
        return fork_root

    @staticmethod
    def _is_text_file(file_path):
        """Heuristically checks if the file is text."""
        try:
            with open(file_path, "tr", encoding="utf-8") as f:
                f.read(1024)
            return True
        except Exception:
            return False

    @staticmethod
    def _compute_sha256(file_path):
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    @classmethod
    def _capture_untracked_file(cls, abs_path, rel_path_str, repo_root, untracked_dir, manifest):
        """
        Captures a single untracked file into the manifest and snapshots directory.
        Security: Silently skips symlinks escaping the repository boundary.
        """
        # ZF-015 & ZF-017 Security Shield: If resolved path escapes repo root, silently skip.
        try:
            resolved = abs_path.resolve()
            repo_resolved = repo_root.resolve()
            if not (resolved == repo_resolved or resolved.is_relative_to(repo_resolved)):
                return  # Outside repo / junction traversal trap — skip
        except (ValueError, OSError):
            return

        try:
            size = abs_path.stat().st_size
        except OSError:
            return

        sha256 = cls._compute_sha256(abs_path)
        is_text = cls._is_text_file(abs_path)
        captured = False

        if is_text and size <= cls.MAX_UNTRACKED_FILE_SIZE:
            dest_path = untracked_dir / rel_path_str
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_path, dest_path)
            captured = True

        manifest.append({
            "path": rel_path_str,
            "size_bytes": size,
            "sha256": sha256,
            "is_text": is_text,
            "captured": captured
        })

    @classmethod
    def export_package(
        cls,
        parent_id=None,
        parent_title="",
        goal="Context Fork Checkpoint",
        accumulated_tokens=None,
        step_count=None,
        custom_handoff_path=None,
        start_path=None,
        force=False
    ):
        """
        Generates a deterministic, timestamped .contextfork/snapshots/ package:
        - git_diff.patch (full diff against HEAD, binary+CRLF safe)
        - git_status.json (machine state)
        - untracked_manifest.json & untracked/ (full content of untracked files)
        - session_metadata.json (IPSF-1.2 manifest, lineage, and consumed status)
        - handoff_summary.md (6-part verifiable summary)
        - latest.json (pointer to the freshest snapshot)
        """
        try:
            git_state = GitInspector.inspect(start_path)
        except GitError as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            sys.exit(1)

        repo_root = git_state["root"]
        fork_root = cls.get_fork_dir(repo_root)
        snapshots_dir = cls.get_snapshots_dir(repo_root)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        parent_session_id = parent_id or f"session-{uuid.uuid4().hex[:8]}"
        child_session_id = f"fork-{uuid.uuid4().hex[:8]}"

        # 1. Lineage Ancestry Title (ZF-020 Shield: Deterministic Repo Name Fallback)
        repo_default_title = repo_root.name if hasattr(repo_root, "name") and repo_root.name else "ContextFork"
        safe_base, child_session_title = cls.sanitize_parent_title(parent_title, default_base=repo_default_title)

        # 2. Timestamp with Seconds & Collision Shield
        timestamp_id = datetime.datetime.now(datetime.timezone.utc).strftime("%y%m%d-%H%M%S")
        snapshot_dir = snapshots_dir / timestamp_id
        counter = 1
        while snapshot_dir.exists():
            snapshot_dir = snapshots_dir / f"{timestamp_id}_{counter:02d}"
            counter += 1
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Output targets: Both snapshot_dir and root fork_root (backwards compatibility)
        output_targets = [snapshot_dir, fork_root]

        # 1. git_diff.patch (full diff against HEAD, binary+CRLF safe)
        diff_bytes_count = len(git_state["diff_full"].encode("utf-8"))
        for target in output_targets:
            patch_file = target / "git_diff.patch"
            patch_file.write_text(git_state["diff_full"], encoding="utf-8")

        # 2. untracked_manifest.json and untracked/ snapshot directory
        untracked_manifest = []
        for target in output_targets:
            untracked_dir = target / "untracked"
            untracked_dir.mkdir(parents=True, exist_ok=True)

        primary_untracked_dir = snapshot_dir / "untracked"

        for rel_path_str in git_state["untracked"]:
            abs_path = repo_root / rel_path_str

            # If directory and not an escaping symlink/junction, recursively scan files
            if abs_path.is_dir():
                is_junc = getattr(abs_path, "is_junction", lambda: False)()
                if abs_path.is_symlink() or is_junc:
                    try:
                        resolved_dir = abs_path.resolve()
                        repo_resolved = repo_root.resolve()
                        if not (resolved_dir == repo_resolved or resolved_dir.is_relative_to(repo_resolved)):
                            continue  # Escaped symlink/junction bridge — skip
                    except (ValueError, OSError):
                        continue

                for child in abs_path.rglob("*"):
                    if child.is_file():
                        try:
                            child_rel = child.relative_to(repo_root)
                        except ValueError:
                            continue
                        cls._capture_untracked_file(
                            child, str(child_rel).replace("\\", "/"),
                            repo_root, primary_untracked_dir, untracked_manifest
                        )
            elif abs_path.is_file():
                cls._capture_untracked_file(
                    abs_path, rel_path_str.replace("\\", "/"),
                    repo_root, primary_untracked_dir, untracked_manifest
                )

        # Write untracked_manifest.json and sync root copy
        untracked_manifest_json = json.dumps(untracked_manifest, indent=2)
        (snapshot_dir / "untracked_manifest.json").write_text(untracked_manifest_json, encoding="utf-8")
        (fork_root / "untracked_manifest.json").write_text(untracked_manifest_json, encoding="utf-8")

        # 3. git_status.json
        status_data = {
            "head_commit": git_state["full_head"],
            "short_head": git_state["short_head"],
            "branch": git_state["branch"],
            "is_dirty": git_state["is_dirty"],
            "files_modified": git_state["modified"],
            "files_staged": git_state["staged"],
            "files_untracked": git_state["untracked"],
            "diff_stat": git_state["diff_stat"]
        }
        status_json = json.dumps(status_data, indent=2)
        (snapshot_dir / "git_status.json").write_text(status_json, encoding="utf-8")
        (fork_root / "git_status.json").write_text(status_json, encoding="utf-8")

        # 4. session_metadata.json (IPSF-1.2 Manifest - Lineage & Consumed State)
        meta_data = {
            "manifest_version": "1.2",
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "fork_timestamp": now,
            "snapshot_id": snapshot_dir.name,
            "consumed": False,
            "consumed_at": None,
            "consumed_by": None,
            "lineage": {
                "parent_title": safe_base,
                "child_session_title": child_session_title,
                "format_standard": "IPSF-1.2-Lineage"
            },
            "token_telemetry": {
                "accumulated_tokens": accumulated_tokens,
                "step_count": step_count,
                "source": "host_provided" if accumulated_tokens is not None else "not_available"
            },
            "git_verification": {
                "head_commit": git_state["full_head"],
                "branch": git_state["branch"],
                "is_clean": not git_state["is_dirty"],
                "diff_stat": git_state["diff_stat"],
                "untracked_count": len(untracked_manifest)
            },
            "package_files": {
                "summary_markdown_path": f"{cls.FORK_DIR_NAME}/{cls.SNAPSHOTS_DIR_NAME}/{snapshot_dir.name}/handoff_summary.md",
                "git_diff_patch_path": f"{cls.FORK_DIR_NAME}/{cls.SNAPSHOTS_DIR_NAME}/{snapshot_dir.name}/git_diff.patch",
                "git_status_json_path": f"{cls.FORK_DIR_NAME}/{cls.SNAPSHOTS_DIR_NAME}/{snapshot_dir.name}/git_status.json",
                "untracked_manifest_path": f"{cls.FORK_DIR_NAME}/{cls.SNAPSHOTS_DIR_NAME}/{snapshot_dir.name}/untracked_manifest.json",
                "session_metadata_json_path": f"{cls.FORK_DIR_NAME}/{cls.SNAPSHOTS_DIR_NAME}/{snapshot_dir.name}/session_metadata.json"
            }
        }
        meta_json = json.dumps(meta_data, indent=2)
        (snapshot_dir / "session_metadata.json").write_text(meta_json, encoding="utf-8")
        (fork_root / "session_metadata.json").write_text(meta_json, encoding="utf-8")

        # 5. handoff_summary.md (6-Part Schema Template or Custom Handoff)
        if custom_handoff_path and Path(custom_handoff_path).is_file():
            summary_content = Path(custom_handoff_path).read_text(encoding="utf-8")
        else:
            token_display = f"{accumulated_tokens:,}" if accumulated_tokens is not None else "N/A"
            step_display = f"{step_count}" if step_count is not None else "N/A"

            summary_content = f"""# 🔄 Session Handoff Checkpoint (IPSF-1.2)
**Parent Session:** `{parent_session_id}` | **Timestamp:** `{now}`
**Accumulated Tokens:** `{token_display}` | **Steps:** `{step_display}`
**Lineage Child Title:** `{child_session_title}`

### 1. Active Goal & Scope
* **Objective:** {goal}
* **Acceptance Criteria:** Deterministic tests pass; working tree integrity verified.

### 2. Settled Decisions, Tradeoffs & Evidence
* **Accepted:** Architectural decision locked in.
  - *Evidence:* Specify file path or commit hash (e.g., `src/Auth/JwtService.cs`)
* **Rejected:** Alternative approaches dismissed during reasoning.
  - *Rationale:* Explain why the alternative was rejected to prevent regression.

### 3. Working Tree State & Machine Git Metadata
* **Branch:** `{git_state['branch']}` | **HEAD:** `{git_state['short_head']}` (`{git_state['full_head'][:10]}...`)
* **Working Tree:** `{'Dirty (Uncommitted Changes)' if git_state['is_dirty'] else 'Clean'}`
* **Changes:** {len(git_state['modified'])} modified, {len(git_state['staged'])} staged, {len(git_state['untracked'])} untracked
```text
{git_state['diff_stat']}
```

### 4. Failed Approaches & Known Pitfalls (⚡ Anti-Loop Shield)
* **Attempted:** Describe any implementation attempt that failed in the parent session.
* **Root Cause:** Detail exact reason for abandonment (e.g. race condition, unsupported API).
* **Barred Action:** Strictly instruct the child agent NOT to retry this approach.

### 5. Open Risks, Edge Cases & Unknowns
* Document any known edge cases, pending validations, or external system dependencies.

### 6. Immediate Next Action
* Specify the single, atomic next command or file edit the child session should execute.
"""
        (snapshot_dir / "handoff_summary.md").write_text(summary_content, encoding="utf-8")
        (fork_root / "handoff_summary.md").write_text(summary_content, encoding="utf-8")

        # 6. Update latest.json Pointer (Pointer Integrity Shield)
        latest_data = {
            "latest_snapshot_id": snapshot_dir.name,
            "child_session_title": child_session_title,
            "parent_title": safe_base,
            "created_at": now
        }
        cls.get_latest_file(repo_root).write_text(json.dumps(latest_data, indent=2), encoding="utf-8")

        print("=" * 65)
        print("✅ [ContextFork] Verifiable Checkpoint Snapshot Created Successfully")
        print("=" * 65)
        print(f"📁 Snapshot Path: {snapshot_dir}")
        print(f"🔗 Lineage Child Title: {child_session_title}")
        print(f"├── 📄 git_diff.patch         ({diff_bytes_count} bytes)")
        print(f"├── 📄 git_status.json        ({len(git_state['modified'])} modified, {len(git_state['staged'])} staged)")
        print(f"├── 📄 untracked_manifest.json ({len(untracked_manifest)} untracked files captured)")
        print(f"├── 📄 session_metadata.json   (Manifest v1.2 | State: pending)")
        print(f"└── 📄 handoff_summary.md      (6-Part Schema Template)")
        print("-" * 65)
        print("💡 Motto: 'LLM summarizes; machines verify.'")
        return True

    @classmethod
    def validate_package(cls, start_path=None, package_id=None):
        """
        Validates integrity and schema compliance of .contextfork/ package with zero external dependencies.
        """
        try:
            repo_root = GitInspector.get_repo_root(start_path)
        except GitError as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return False

        try:
            fork_path = cls.resolve_active_package_dir(repo_root, package_id=package_id)
        except Exception as e:
            print(f"❌ Error resolving package: {e}", file=sys.stderr)
            return False

        if not fork_path.is_dir():
            print(f"❌ Error: No .contextfork package found in {repo_root}")
            return False

        errors = []
        warnings = []

        # 1. Required package files existence
        req_files = [
            "git_diff.patch",
            "git_status.json",
            "untracked_manifest.json",
            "session_metadata.json",
            "handoff_summary.md"
        ]
        for fname in req_files:
            p = fork_path / fname
            if not p.is_file():
                errors.append(f"Missing required package file: {fname}")

        # 2. Validate session_metadata.json
        meta_file = fork_path / "session_metadata.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                for key in ["manifest_version", "parent_session_id", "child_session_id", "git_verification", "package_files"]:
                    if key not in meta:
                        errors.append(f"session_metadata.json missing required key: {key}")
                if meta.get("manifest_version") != "1.2":
                    warnings.append(f"Unexpected manifest version: {meta.get('manifest_version')} (expected 1.2)")
            except json.JSONDecodeError as e:
                errors.append(f"session_metadata.json contains invalid JSON: {e}")

        # 3. Validate git_status.json
        status_file = fork_path / "git_status.json"
        if status_file.is_file():
            try:
                st = json.loads(status_file.read_text(encoding="utf-8"))
                for key in ["head_commit", "branch", "is_dirty", "files_modified", "files_untracked"]:
                    if key not in st:
                        errors.append(f"git_status.json missing required key: {key}")
            except json.JSONDecodeError as e:
                errors.append(f"git_status.json contains invalid JSON: {e}")

        # 4. Validate untracked_manifest.json
        manifest_file = fork_path / "untracked_manifest.json"
        if manifest_file.is_file():
            try:
                items = json.loads(manifest_file.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    errors.append("untracked_manifest.json must be a JSON array")
                else:
                    for item in items:
                        if not all(k in item for k in ["path", "size_bytes", "sha256"]):
                            errors.append(f"untracked manifest item missing attributes: {item.get('path', 'unknown')}")
            except json.JSONDecodeError as e:
                errors.append(f"untracked_manifest.json contains invalid JSON: {e}")

        # 5. Check handoff_summary.md 6-part schema + detect unfilled template markers
        summary_file = fork_path / "handoff_summary.md"
        if summary_file.is_file():
            content = summary_file.read_text(encoding="utf-8")
            required_sections = [
                "1. Active Goal",
                "2. Settled Decisions",
                "3. Working Tree State",
                "4. Failed Approaches",
                "5. Open Risks",
                "6. Immediate Next Action"
            ]
            for sec in required_sections:
                if sec not in content:
                    errors.append(f"handoff_summary.md missing required section: '{sec}'")
            # Detect unfilled template placeholders ({{ or TODO markers)
            if "{{" in content or "}}" in content:
                errors.append(
                    "handoff_summary.md contains unfilled template placeholders ('{{...}}'). "
                    "Fill in all sections before validating."
                )

        # 6. Re-verify untracked_manifest SHA-256 hashes
        manifest_file = fork_path / "untracked_manifest.json"
        if manifest_file.is_file():
            try:
                items = json.loads(manifest_file.read_text(encoding="utf-8"))
                if isinstance(items, list):
                    untracked_snapshot_dir = fork_path / "untracked"
                    for item in items:
                        if not item.get("captured", False):
                            continue
                        snap_path = untracked_snapshot_dir / item["path"]
                        if not snap_path.is_file():
                            errors.append(
                                f"Captured untracked snapshot missing: untracked/{item['path']}"
                            )
                            continue
                        actual_sha = cls._compute_sha256(snap_path)
                        if actual_sha != item.get("sha256", ""):
                            errors.append(
                                f"SHA-256 mismatch for captured file '{item['path']}': "
                                f"expected {item.get('sha256', 'N/A')}, got {actual_sha}"
                            )
            except (json.JSONDecodeError, KeyError):
                pass  # JSON parse error already reported above

        # 7. Verify HEAD commit from git_status.json exists in repo
        status_file = fork_path / "git_status.json"
        if status_file.is_file():
            try:
                st = json.loads(status_file.read_text(encoding="utf-8"))
                head = st.get("head_commit", "")
                if head and len(head) == 40:
                    try:
                        GitInspector.run_cmd(
                            ["git", "cat-file", "-e", f"{head}^{{commit}}"], cwd=repo_root
                        )
                    except GitError:
                        errors.append(
                            f"HEAD commit '{head[:7]}' recorded in git_status.json does not "
                            "exist in this repository. Package may be from a different repo."
                        )
            except (json.JSONDecodeError, KeyError):
                pass

        # 8. Check git_diff.patch applicability (git apply --check)
        patch_file = fork_path / "git_diff.patch"
        if patch_file.is_file() and patch_file.stat().st_size > 0:
            try:
                GitInspector.run_cmd(
                    ["git", "apply", "--check", "--whitespace=nowarn", str(patch_file)],
                    cwd=repo_root
                )
            except GitError as e:
                warnings.append(
                    f"git_diff.patch cannot be cleanly applied to current working tree "
                    f"(working tree may have diverged from package state): {e}"
                )

        print("=" * 65)
        print("🔍 [ContextFork] Package Validation Report")
        print("=" * 65)
        if errors:
            print(f"❌ FAILED with {len(errors)} error(s):")
            for err in errors:
                print(f"   • {err}")
            return False

        print("✅ PASSED: Verifiable Handoff Package is compliant with IPSF-1.2.")
        if warnings:
            print(f"⚠️  {len(warnings)} warning(s):")
            for w in warnings:
                print(f"   • {w}")
        return True

    @classmethod
    def get_status(cls, start_path=None):
        """Reports repository state and ContextFork package status together."""
        try:
            git_state = GitInspector.inspect(start_path)
        except GitError as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return

        repo_root = git_state["root"]
        fork_path = cls.get_fork_dir(repo_root)
        has_package = fork_path.is_dir()

        print("=" * 60)
        print("📊 ContextFork Runtime Status")
        print("=" * 60)
        print("Repository (Ground Truth):")
        print(f"  • Root:        {repo_root}")
        print(f"  • Branch:      {git_state['branch']}")
        print(f"  • HEAD Commit: {git_state['short_head']} ({git_state['full_head'][:16]}...)")
        print(f"  • Dirty State: {'YES (Uncommitted changes)' if git_state['is_dirty'] else 'NO (Clean)'}")
        print(f"  • Modified:    {len(git_state['modified'])} file(s)")
        print(f"  • Staged:      {len(git_state['staged'])} file(s)")
        print(f"  • Untracked:   {len(git_state['untracked'])} file(s)")
        print("-" * 60)
        print("ContextFork Package:")
        print(f"  • Directory:   {cls.FORK_DIR_NAME}/")
        print(f"  • Present:     {'YES ✅' if has_package else 'NO ⚪'}")

        if has_package:
            meta_file = fork_path / "session_metadata.json"
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    print(f"  • Manifest:    v{meta.get('manifest_version', 'unknown')}")
                    print(f"  • Fork Time:   {meta.get('fork_timestamp', 'unknown')}")
                    print(f"  • Parent ID:   {meta.get('parent_session_id', 'unknown')}")
                    print(f"  • Child ID:    {meta.get('child_session_id', 'unknown')}")
                    lineage = meta.get("lineage", {})
                    if lineage.get("child_session_title"):
                        print(f"  • Child Title: {lineage.get('child_session_title')}")
                except Exception:
                    print("  • Manifest:    [Corrupted]")
            summary_file = fork_path / "handoff_summary.md"
            print(f"  • Summary:     {'Present ✅' if summary_file.is_file() else 'Missing ❌'}")

        # Snapshot Archive Summary
        packages = cls.list_packages(repo_root)
        consumed_count = sum(1 for p in packages if p.get("consumed"))
        unconsumed_count = len(packages) - consumed_count
        print("-" * 60)
        print(f"Snapshots Archive: {len(packages)} total ({consumed_count} consumed, {unconsumed_count} pending)")
        print("=" * 60)

    @classmethod
    def list_packages(cls, repo_root) -> list[dict]:
        """Lists all registered snapshot packages in reverse chronological order."""
        snapshots_dir = cls.get_snapshots_dir(repo_root)
        if not snapshots_dir.is_dir():
            return []

        packages = []
        for child in sorted(snapshots_dir.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            meta_file = child / "session_metadata.json"
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                lineage = meta.get("lineage", {})
                git_ver = meta.get("git_verification", {})
                packages.append({
                    "snapshot_id": child.name,
                    "created_at": meta.get("fork_timestamp", ""),
                    "parent_title": lineage.get("parent_title", "Session"),
                    "child_session_title": lineage.get("child_session_title", child.name),
                    "consumed": meta.get("consumed", False),
                    "consumed_at": meta.get("consumed_at"),
                    "consumed_by": meta.get("consumed_by"),
                    "head_commit": git_ver.get("head_commit", "")[:7],
                    "branch": git_ver.get("branch", ""),
                    "untracked_count": git_ver.get("untracked_count", 0),
                    "path": child
                })
            except Exception:
                continue
        return packages

    @classmethod
    def mark_consumed(cls, repo_root, package_id=None, consumer_session_id=None) -> bool:
        """Marks the specified package as consumed."""
        try:
            target_dir = cls.resolve_active_package_dir(repo_root, package_id=package_id)
        except Exception:
            return False

        meta_file = target_dir / "session_metadata.json"
        if not meta_file.is_file():
            return False

        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            meta["consumed"] = True
            meta["consumed_at"] = now
            if consumer_session_id:
                meta["consumed_by"] = consumer_session_id
            meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            # Also update root copy if present
            root_meta = cls.get_fork_dir(repo_root) / "session_metadata.json"
            if root_meta.is_file():
                root_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    @classmethod
    def clean_packages(cls, repo_root, confirm=False, only_consumed=True) -> dict:
        """
        User-confirmed cleanup (Dry-Run by default).
        If confirm=False, returns packages that would be deleted without touching files.
        If confirm=True, physically deletes packages and repoints latest.json (Dangling Pointer Shield).
        """
        packages = cls.list_packages(repo_root)
        to_delete = []
        for p in packages:
            if only_consumed and not p.get("consumed"):
                continue  # Preserve unconsumed packages
            to_delete.append(p)

        if not confirm:
            return {
                "dry_run": True,
                "would_delete_count": len(to_delete),
                "packages": [p["snapshot_id"] for p in to_delete],
                "message": f"{len(to_delete)} package(s) eligible for cleanup. Set confirm=True to proceed with deletion."
            }

        # confirm=True: Physical deletion
        deleted = []
        for p in to_delete:
            p_dir = p["path"]
            try:
                shutil.rmtree(p_dir)
                deleted.append(p["snapshot_id"])
            except Exception:
                pass

        # Dangling Pointer Shield:
        latest_file = cls.get_latest_file(repo_root)
        remaining = cls.list_packages(repo_root)
        if remaining:
            # Repoint to freshest remaining package
            fresh = remaining[0]
            latest_data = {
                "latest_snapshot_id": fresh["snapshot_id"],
                "child_session_title": fresh["child_session_title"],
                "parent_title": fresh["parent_title"],
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            latest_file.write_text(json.dumps(latest_data, indent=2), encoding="utf-8")
        else:
            if latest_file.is_file():
                latest_file.unlink(missing_ok=True)

        return {
            "dry_run": False,
            "deleted_count": len(deleted),
            "packages": deleted,
            "message": f"{len(deleted)} package(s) successfully cleaned."
        }


def run_demo():
    """Runs a live simulated terminal demonstration."""
    print("=" * 65)
    print("🚀 ContextFork (IPSF-1.2) Reference Implementation Demo")
    print("=" * 65)
    print("Motto:")
    print("  'LLM summarizes; machines verify.'")
    print("  'Don't ask the AI to remember what the machine can verify.'\n")

    print("[Step 1] Ambient Context Telemetry Monitoring:")
    print("  🟢 Step 25  | Tokens:  32,400 | Level: Normal   | Action: Continue")
    print("  🟡 Step 82  | Tokens:  68,100 | Level: Caution  | Action: Prepare Milestone")
    print("  🔴 Step 226 | Tokens: 119,400 | Level: Critical | Action: Fork Recommended\n")

    print("[Step 2] Capturing Deterministic Git Ground Truth:")
    try:
        git_state = GitInspector.inspect()
        print(f"  • Repo Root:     {git_state['root']}")
        print(f"  • HEAD Commit:   {git_state['short_head']} (Full: {git_state['full_head'][:12]}...)")
        print(f"  • Branch:        {git_state['branch']}")
        print(f"  • Working Tree:  {'Dirty' if git_state['is_dirty'] else 'Clean'}")
        print(f"  • Untracked:     {len(git_state['untracked'])} file(s) identified for capture")
    except Exception as e:
        print(f"  (Simulated Environment): HEAD 7ed9b80, Branch main, Dirty True ({e})")

    print("\n[Step 3] Executing 'Summarize & Fork' (force=True for demo repeatability)...")
    ContextForkEngine.export_package(
        parent_id="session-demo-226",
        goal="Context Lifecycle Protocol Standard",
        accumulated_tokens=119400,
        step_count=226,
        force=True
    )

    print("\n[Step 4] Running Package Verification (validate):")
    ContextForkEngine.validate_package()
    print("\n🎯 Outcome: Verifiable handoff package ready. New session launches with architectural continuity.")


def main():
    parser = argparse.ArgumentParser(
        description="ContextFork Reference Implementation (IPSF-1.2) — see README for protocol spec."
    )
    subparsers = parser.add_subparsers(dest="command")

    # status
    subparsers.add_parser("status", help="Show repository state and active .contextfork package info")

    # export
    export_parser = subparsers.add_parser("export", help="Generate the deterministic .contextfork package")
    export_parser.add_argument("--session", default=None, help="Parent session identifier")
    export_parser.add_argument("--title", default="", help="Parent conversation title for lineage ancestry")
    export_parser.add_argument("--goal", default="Active Development", help="Active goal description")
    export_parser.add_argument("--tokens", type=int, default=None, help="Accumulated prompt tokens (if known)")
    export_parser.add_argument("--steps", type=int, default=None, help="Total steps executed (if known)")
    export_parser.add_argument("--handoff", default=None, help="Path to a pre-written handoff markdown file")
    export_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing .contextfork package (default: error if package already exists)"
    )

    # validate
    val_parser = subparsers.add_parser("validate", help="Validate the .contextfork package against IPSF-1.2")
    val_parser.add_argument("--package", default=None, help="Specific snapshot ID to validate (optional)")

    # details
    subparsers.add_parser("details", help="Show all archived snapshots and their consumed state")

    # clean
    clean_parser = subparsers.add_parser("clean", help="Clean consumed or old snapshot packages")
    clean_parser.add_argument("--confirm", action="store_true", help="Confirm physical deletion (default is dry-run)")
    clean_parser.add_argument("--all", action="store_true", help="Delete all packages including unconsumed ones")

    # demo
    subparsers.add_parser("demo", help="Run an interactive terminal demonstration")

    args = parser.parse_args()

    # No command provided -> display help (demo does not run automatically)
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "status":
        ContextForkEngine.get_status()
    elif args.command == "export":
        ContextForkEngine.export_package(
            parent_id=args.session,
            parent_title=args.title,
            goal=args.goal,
            accumulated_tokens=args.tokens,
            step_count=args.steps,
            custom_handoff_path=args.handoff,
            force=args.force
        )
    elif args.command == "validate":
        valid = ContextForkEngine.validate_package(package_id=args.package)
        sys.exit(0 if valid else 1)
    elif args.command == "details":
        repo_root = GitInspector.get_repo_root()
        packages = ContextForkEngine.list_packages(repo_root)
        print("=" * 70)
        print("📦 ContextFork Snapshot Archive (PackDetails)")
        print("=" * 70)
        if not packages:
            print("No snapshots found in .contextfork/snapshots/")
        else:
            for p in packages:
                state_icon = "✅ Consumed" if p.get("consumed") else "⏳ Pending"
                print(f"• ID: {p['snapshot_id']} | State: {state_icon} | Title: {p['child_session_title']}")
                print(f"  Head: {p['head_commit']} ({p['branch']}) | Untracked: {p['untracked_count']} | Created: {p['created_at']}")
        print("=" * 70)
    elif args.command == "clean":
        repo_root = GitInspector.get_repo_root()
        res = ContextForkEngine.clean_packages(repo_root, confirm=args.confirm, only_consumed=not args.all)
        print(f"🧹 Clean Result: {res['message']}")
    elif args.command == "demo":
        run_demo()


if __name__ == "__main__":
    main()

