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
    """Git operasyonları için temel hata sınıfı."""
    pass


class NotAGitRepositoryError(GitError):
    """Çalışma dizini bir Git deposu olmadığında fırlatılır."""
    pass


class GitNotFoundError(GitError):
    """Sistemde git CLI bulunamadığında fırlatılır."""
    pass


class GitInspector:
    """Yerel Git çalışma alanını deterministik olarak sorgulayan araç."""

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
        """Her zaman deponun en üst kök dizinini (--show-toplevel) döndürür."""
        out = cls.run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=start_path)
        return Path(out.strip()).resolve()

    @classmethod
    def inspect(cls, start_path=None):
        """Deponun tüm anlık durumunu deterministik olarak çıkarır."""
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

        # 4. Diff HEAD (Hem staged hem unstaged tüm değişiklikleri kapsar)
        # --binary: CRLF ve binary dosyaların patch'te korunması (Windows güvenliği)
        # -c color.diff=false: ANSI renk kodlarının patch'i bozmasını engeller
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
    """Protokol paketlerini oluşturan, doğrulayan ve yöneten motor."""

    FORK_DIR_NAME = ".contextfork"
    MAX_UNTRACKED_FILE_SIZE = 1024 * 1024  # 1 MB sınır (büyük binary çöplerini önlemek için)

    @classmethod
    def get_fork_dir(cls, repo_root):
        return repo_root / cls.FORK_DIR_NAME

    @staticmethod
    def _is_text_file(file_path):
        """Basit bir sezgisel yöntemle dosyanın metin olup olmadığını kontrol eder."""
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
        Tek bir untracked dosyayı manifest'e ekler ve snapshot'ını alır.
        Güvenlik: repo sınırı dışına çıkan symlink'ler sessizce atlanır.
        """
        # Symlink güvenliği: hedef repo dışındaysa atla
        if abs_path.is_symlink():
            try:
                resolved = abs_path.resolve()
                repo_resolved = repo_root.resolve()
                resolved.relative_to(repo_resolved)  # Repo içindeyse geçer, değilse ValueError
            except ValueError:
                return  # Repo dışı symlink — atla

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
        goal="Context Fork Checkpoint",
        accumulated_tokens=None,
        step_count=None,
        custom_handoff_path=None,
        start_path=None,
        force=False
    ):
        """
        Deterministik .contextfork/ paketini oluşturur:
        - git_diff.patch (HEAD'e karşı tam fark, binary+CRLF güvenli)
        - git_status.json (makine durumu)
        - untracked_manifest.json & untracked/ (yeni dosyaların tam içeriği, nested dizinler dahil)
        - session_metadata.json (protokol manifesti)
        - handoff_summary.md (6 parçalı kanıtlı özet)
        """
        try:
            git_state = GitInspector.inspect(start_path)
        except GitError as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            sys.exit(1)

        repo_root = git_state["root"]
        fork_path = cls.get_fork_dir(repo_root)

        # Mevcut paket varsa --force olmadan dur
        if fork_path.exists() and not force:
            print(
                f"❌ Error: A .contextfork package already exists at {fork_path}.\n"
                "   Use --force to overwrite it.",
                file=sys.stderr
            )
            sys.exit(1)

        if fork_path.exists():
            shutil.rmtree(fork_path)
        fork_path.mkdir(parents=True, exist_ok=True)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        parent_session_id = parent_id or f"session-{uuid.uuid4().hex[:8]}"
        child_session_id = f"fork-{uuid.uuid4().hex[:8]}"

        # 1. git_diff.patch (HEAD'e karşı staged + unstaged, binary+CRLF güvenli)
        patch_file = fork_path / "git_diff.patch"
        patch_file.write_text(git_state["diff_full"], encoding="utf-8")

        # 2. untracked_manifest.json ve untracked/ snapshot klasörü
        # (git diff HEAD'in kapsamadığı yeni dosyaların kurtarılması)
        # Nested untracked dizinler dahil rekürsif tarama
        untracked_manifest = []
        untracked_dir = fork_path / "untracked"

        for rel_path_str in git_state["untracked"]:
            abs_path = repo_root / rel_path_str

            # Dizinse içindeki tüm dosyaları rekürsif tara
            if abs_path.is_dir():
                for child in abs_path.rglob("*"):
                    if child.is_file():
                        child_rel = child.relative_to(repo_root)
                        cls._capture_untracked_file(
                            child, str(child_rel).replace("\\", "/"),
                            repo_root, untracked_dir, untracked_manifest
                        )
            elif abs_path.is_file():
                cls._capture_untracked_file(
                    abs_path, rel_path_str.replace("\\", "/"),
                    repo_root, untracked_dir, untracked_manifest
                )
            # Symlink veya bilinmeyen türleri atla (güvenlik)

        untracked_manifest_file = fork_path / "untracked_manifest.json"
        untracked_manifest_file.write_text(json.dumps(untracked_manifest, indent=2), encoding="utf-8")

        # 3. git_status.json
        status_file = fork_path / "git_status.json"
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
        status_file.write_text(json.dumps(status_data, indent=2), encoding="utf-8")

        # 4. session_metadata.json (IPSF-1.2 Manifest)
        meta_file = fork_path / "session_metadata.json"
        meta_data = {
            "manifest_version": "1.2",
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "fork_timestamp": now,
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
                "summary_markdown_path": f"{cls.FORK_DIR_NAME}/handoff_summary.md",
                "git_diff_patch_path": f"{cls.FORK_DIR_NAME}/git_diff.patch",
                "git_status_json_path": f"{cls.FORK_DIR_NAME}/git_status.json",
                "untracked_manifest_path": f"{cls.FORK_DIR_NAME}/untracked_manifest.json",
                "session_metadata_json_path": f"{cls.FORK_DIR_NAME}/session_metadata.json"
            }
        }
        meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

        # 5. handoff_summary.md (6 Parçalı Şablon veya Özel Handoff)
        summary_file = fork_path / "handoff_summary.md"
        if custom_handoff_path and Path(custom_handoff_path).is_file():
            summary_content = Path(custom_handoff_path).read_text(encoding="utf-8")
        else:
            token_display = f"{accumulated_tokens:,}" if accumulated_tokens is not None else "N/A"
            step_display = f"{step_count}" if step_count is not None else "N/A"

            summary_content = f"""# 🔄 Session Handoff Checkpoint (IPSF-1.2)
**Parent Session:** `{parent_session_id}` | **Timestamp:** `{now}`
**Accumulated Tokens:** `{token_display}` | **Steps:** `{step_display}`

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
        summary_file.write_text(summary_content, encoding="utf-8")

        print("=" * 65)
        print("✅ [ContextFork] Verifiable Handoff Package Created Successfully")
        print("=" * 65)
        print(f"📁 Package Root: {fork_path}")
        print(f"├── 📄 git_diff.patch         ({len(git_state['diff_full'])} bytes)")
        print(f"├── 📄 git_status.json        ({len(git_state['modified'])} modified, {len(git_state['staged'])} staged)")
        print(f"├── 📄 untracked_manifest.json ({len(untracked_manifest)} untracked files captured)")
        print(f"├── 📄 session_metadata.json   (Manifest v1.2 | HEAD: {git_state['short_head']})")
        print(f"└── 📄 handoff_summary.md      (6-Part Schema Template)")
        print("-" * 65)
        print("💡 Motto: 'LLM summarizes; machines verify.'")
        return True

    @classmethod
    def validate_package(cls, start_path=None):
        """
        Sıfır bağımlılıkla .contextfork/ paketinin bütünlüğünü ve şema uyumunu denetler.
        """
        try:
            repo_root = GitInspector.get_repo_root(start_path)
        except GitError as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return False

        fork_path = cls.get_fork_dir(repo_root)
        if not fork_path.is_dir():
            print(f"❌ Error: No .contextfork package found in {repo_root}")
            return False

        errors = []
        warnings = []

        # 1. Gerekli dosyaların varlığı
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

        # 2. session_metadata.json doğrulaması
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

        # 3. git_status.json doğrulaması
        status_file = fork_path / "git_status.json"
        if status_file.is_file():
            try:
                st = json.loads(status_file.read_text(encoding="utf-8"))
                for key in ["head_commit", "branch", "is_dirty", "files_modified", "files_untracked"]:
                    if key not in st:
                        errors.append(f"git_status.json missing required key: {key}")
            except json.JSONDecodeError as e:
                errors.append(f"git_status.json contains invalid JSON: {e}")

        # 4. untracked_manifest.json doğrulaması
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

        # 5. handoff_summary.md 6-parçalı yapı kontrolü + doldurulmamış şablon tespiti
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
            # Doldurulmamış şablon tespiti ({{ veya TODO işaretçiler)
            if "{{" in content or "}}" in content:
                errors.append(
                    "handoff_summary.md contains unfilled template placeholders ('{{...}}'). "
                    "Fill in all sections before validating."
                )

        # 6. untracked_manifest SHA-256 yeniden doğrulama
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

        # 7. git_status.json'daki HEAD commit'in repoda varlığını doğrula
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

        # 8. git_diff.patch uygulanabilirlik kontrolü (git apply --check)
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
        """Repository ve ContextFork paket durumunu birlikte raporlar."""
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
                except Exception:
                    print("  • Manifest:    [Corrupted]")
            summary_file = fork_path / "handoff_summary.md"
            print(f"  • Summary:     {'Present ✅' if summary_file.is_file() else 'Missing ❌'}")
        print("=" * 60)


def run_demo():
    """Terminalde canlı ve renkli bir simülasyon çalıştırır."""
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
    export_parser.add_argument("--goal", default="Active Development", help="Active goal description")
    export_parser.add_argument("--tokens", type=int, default=None, help="Accumulated prompt tokens (if known)")
    export_parser.add_argument("--steps", type=int, default=None, help="Total steps executed (if known)")
    export_parser.add_argument("--handoff", default=None, help="Path to a pre-written handoff markdown file")
    export_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing .contextfork package (default: error if package already exists)"
    )

    # validate
    subparsers.add_parser("validate", help="Validate the .contextfork package against IPSF-1.2")

    # demo
    subparsers.add_parser("demo", help="Run an interactive terminal demonstration")

    args = parser.parse_args()

    # Argümansız çalışma → help göster (demo otomatik ÇALIŞMAZ)
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "status":
        ContextForkEngine.get_status()
    elif args.command == "export":
        ContextForkEngine.export_package(
            parent_id=args.session,
            goal=args.goal,
            accumulated_tokens=args.tokens,
            step_count=args.steps,
            custom_handoff_path=args.handoff,
            force=args.force
        )
    elif args.command == "validate":
        valid = ContextForkEngine.validate_package()
        sys.exit(0 if valid else 1)
    elif args.command == "demo":
        run_demo()


if __name__ == "__main__":
    main()

