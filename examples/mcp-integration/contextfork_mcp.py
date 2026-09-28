#!/usr/bin/env python3
"""
ContextFork MCP Server (IPSF-1.2) - Hardened Implementation
Model Context Protocol reference integration for native in-place session forking.

Enables AI agents (Claude Desktop, Cursor, Antigravity, etc.) to monitor context
telemetry, trigger verifiable handoff checkpoints, and seamlessly resume continuation sessions.

Author: Mustafa KILINC (@mrblackman)
License: MIT
"""

import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Safeguard console encodings on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Compatible FastMCP import (MCP 1.x and MCP 2.x)
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "❌ Error: 'mcp' SDK is required for this example.\n"
            "   Install via: pip install mcp",
            file=sys.stderr
        )
        sys.exit(1)

# Locate repository root and contextfork.py reference CLI
# Dynamic CLI path resolution with sibling file priority
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

def _is_git_repository(p: Path) -> bool:
    """Check if a path is the exact root of a git repository (timeout=5s)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=5.0
        )
        if r.returncode == 0:
            toplevel = Path(r.stdout.strip()).resolve()
            return os.path.normcase(str(p.resolve())) == os.path.normcase(str(toplevel))
        return False
    except Exception:
        return False


cli_override = os.environ.get("CONTEXTFORK_CLI_PATH")
if cli_override and Path(cli_override).is_file():
    CLI_PATH = Path(cli_override).resolve()
elif (Path(__file__).resolve().parent / "contextfork.py").is_file():
    CLI_PATH = (Path(__file__).resolve().parent / "contextfork.py").resolve()
elif _is_git_repository(DEFAULT_REPO_ROOT) and (DEFAULT_REPO_ROOT / "contextfork.py").is_file():
    # Development fallback ONLY if DEFAULT_REPO_ROOT is a verified git repo
    CLI_PATH = (DEFAULT_REPO_ROOT / "contextfork.py").resolve()
else:
    raise FileNotFoundError("Cannot find contextfork.py reference CLI at expected paths.")


def validate_and_parse_allowed_roots(
    env_str: str | None = None,
    default_root: Path | None = None
) -> tuple[tuple[Path, ...], str | None]:
    """
    ZF-026 Allowed Roots Security Contract:
    Strictly validates and parses CONTEXTFORK_ALLOWED_ROOTS environment variable.
    Returns (roots_tuple, warning_message).
    Fail-closed: If ANY entry is invalid, the entire env variable is discarded,
    falling back strictly to default_root (if git repo).
    """
    valid_default: list[Path] = []
    if default_root is not None:
        cand_def = default_root.resolve()
        if _is_git_repository(cand_def):
            valid_default.append(cand_def)

    if env_str is None:
        env_str = os.environ.get("CONTEXTFORK_ALLOWED_ROOTS", "")

    raw_val = env_str.strip()
    if not raw_val:
        if default_root is not None and not valid_default:
            return (), f"⚠️ Default root ({default_root}) is not a Git repository and CONTEXTFORK_ALLOWED_ROOTS is not defined."
        return tuple(valid_default), None

    # 1. Platform-independent fixed delimiter: ';'
    entries = [e.strip() for e in raw_val.split(";")]
    entries = [e for e in entries if e]

    if not entries:
        if default_root is not None and not valid_default:
            return (), f"⚠️ Default root ({default_root}) is not a Git repository and CONTEXTFORK_ALLOWED_ROOTS is not defined."
        return tuple(valid_default), None

    parsed_roots: list[Path] = []
    seen_norms: set[str] = set()

    for raw in entries:
        # 3. Must be absolute path; relative paths fail
        p_raw = Path(raw)
        if not p_raw.is_absolute():
            msg = f"Relative path rejected (absolute path required): '{raw}'"
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"

        # 4. Traversal '..' check before resolve()
        if ".." in p_raw.parts:
            msg = f"Traversal ('..') component rejected: '{raw}'"
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"

        # 5. UNC and device paths check (NTLM credential leak shield)
        if raw.startswith(("\\\\", "//")) or re.match(r"^[\\/]{2}", raw) or p_raw.drive.startswith(("\\\\", "//")):
            msg = f"UNC / network path or device path rejected: '{raw}'"
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"

        # 6. Must exist and be a directory
        try:
            resolved = p_raw.resolve()
        except Exception as e:
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: Could not resolve '{raw}': {e}"

        if not resolved.is_dir():
            msg = f"Directory not found or not a directory: '{raw}'"
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"

        # 7. Overly broad roots check (Drive root or user home and above)
        if resolved == Path(resolved.drive + "\\") or resolved == Path(resolved.drive + "/") or len(resolved.parts) <= 1:
            msg = f"Overly broad drive root rejected: '{raw}'"
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"

        try:
            user_home = Path.home().resolve()
            if resolved == user_home or user_home.is_relative_to(resolved):
                msg = f"Overly broad user home directory / ancestor rejected: '{raw}'"
                return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"
        except Exception:
            pass

        # 8. Junction / Symlink shield: normcase comparison
        raw_norm = os.path.normcase(str(p_raw))
        resolved_norm = os.path.normcase(str(resolved))
        if raw_norm != resolved_norm:
            msg = f"Junction/symlink root rejected (specify real target path): '{raw}' -> '{resolved}'"
            return tuple(valid_default), f"⚠️ CONTEXTFORK_ALLOWED_ROOTS rejected: {msg}"

        if resolved_norm not in seen_norms:
            seen_norms.add(resolved_norm)
            parsed_roots.append(resolved)

    all_roots = list(parsed_roots)
    for d in valid_default:
        d_norm = os.path.normcase(str(d))
        if d_norm not in seen_norms:
            seen_norms.add(d_norm)
            all_roots.append(d)

    return tuple(all_roots), None


# Initialize ALLOWED_ROOTS and warning at module load (Immutable)
ALLOWED_ROOTS, ALLOWED_ROOTS_WARNING = validate_and_parse_allowed_roots(default_root=DEFAULT_REPO_ROOT)
if ALLOWED_ROOTS_WARNING:
    print(f"[ContextFork] {ALLOWED_ROOTS_WARNING}", file=sys.stderr)

# Initialize MCP Server instance
app = FastMCP(
    "ContextFork",
    instructions=(
        "ContextFork (IPSF-1.2) provides deterministic, machine-verifiable session "
        "handoffs when LLM context horizons balloon. Use 'export_session_context' "
        "to snapshot work, and 'resume_session_context' in fresh continuation sessions."
    )
)


def _validate_and_resolve_workspace(raw_path: str = "") -> Path:
    """
    Validates target directory against ALLOWED_ROOTS, checks git status,
    and defends against Sibling Traversal, Path Traversal, and Junction loops.
    """
    if not ALLOWED_ROOTS:
        warn = f" ({ALLOWED_ROOTS_WARNING})" if ALLOWED_ROOTS_WARNING else ""
        raise PermissionError(f"Access Denied: ALLOWED_ROOTS is empty{warn}. Please configure CONTEXTFORK_ALLOWED_ROOTS.")

    target = Path(raw_path).resolve() if raw_path.strip() else ALLOWED_ROOTS[0]

    # 1. Sibling Traversal Shield (ZF-014)
    if not any(target == root or target.is_relative_to(root) for root in ALLOWED_ROOTS):
        raise PermissionError(f"Access Denied: Path '{target}' is outside ALLOWED_ROOTS.")

    # 2. Git repo verification
    check = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )
    if check.returncode != 0:
        err_msg = check.stderr.strip()
        if "dubious ownership" in err_msg.lower():
            raise PermissionError(
                f"Access Denied: You do not have permission to access the target directory ({target}) with this user account (Git dubious ownership).\n"
                f"Detail: {err_msg}"
            )
        raise ValueError(f"Target directory is not a valid Git repository ({target}): {err_msg}")

    # 3. Unchecked Git Toplevel Shield (ZF-016)
    resolved_repo = Path(check.stdout.strip()).resolve()
    if not any(resolved_repo == root or resolved_repo.is_relative_to(root) for root in ALLOWED_ROOTS):
        raise PermissionError(f"Access Denied: Git root '{resolved_repo}' escapes ALLOWED_ROOTS.")

    return resolved_repo


def _run_cli_command(args: list[str], target_cwd: Path) -> tuple[int, str, str]:
    """Execute contextfork.py in an isolated subprocess with explicit working directory."""
    cmd = [sys.executable, str(CLI_PATH)] + args
    res = subprocess.run(
        cmd,
        cwd=str(target_cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )
    return res.returncode, res.stdout, res.stderr


def _resolve_conversation_title(target_dir: Path, session_id: str = "") -> str:
    """
    Resolve the active conversation title using layered fallback:
    1. Check Host Signal: Only query SQLite if session_id is provided
       or CONTEXTFORK_HOST is set to 'antigravity'. Otherwise fallback to resolved_repo.name.
    2. If session_id provided, look up exact conversation in SQLite (title or preview).
       If title/preview is empty, short-circuit directly to fallback_title.
    3. If session_id is empty, query recent conversations in workspace (title or preview).
    4. Strict ZF-024 Sibling Traversal Shield: Parse URIs with unquote + url2pathname + normcase
       and verify workspace is equal to or inside target_dir.
    5. Cleanse whitespaces, CRLF, tabs, and null bytes (ZF-020 Sanitization).
    6. Stdio JSON-RPC safe: errors output single-line diagnostic to stderr only.
    """
    fallback_title = target_dir.name

    # 1. Host Signal Check (Cross-Host Context Bleed Shield)
    has_host_signal = bool(session_id and session_id.strip()) or (os.environ.get("CONTEXTFORK_HOST", "").lower() == "antigravity")
    if not has_host_signal:
        return fallback_title

    try:
        # Dynamic profile discovery & Test Fixture Injection
        db_override = os.environ.get("CONTEXTFORK_DB_PATH")
        if db_override and Path(db_override).is_file():
            candidate_paths = [Path(db_override)]
        else:
            candidate_paths = [
                Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
            ]

        target_norm = os.path.normcase(str(target_dir.resolve()))
        target_path = target_dir.resolve()

        for db_path in candidate_paths:
            if not db_path.is_file():
                continue

            # ZF-023: Open in read-only URI mode with 5.0s timeout to avoid lock contention
            db_uri = f"file:{db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(db_uri, uri=True, timeout=5.0)
            try:
                cursor = conn.cursor()

                # Priority 1: Exact match by session_id!
                if session_id and session_id.strip():
                    cursor.execute(
                        "SELECT title, preview, workspace_uris FROM conversation_summaries WHERE conversation_id = ?",
                        (session_id.strip(),)
                    )
                    row = cursor.fetchone()
                    if row:
                        t, prev, uris = row
                        candidate = (t or "").strip() or (prev or "").strip()
                        if candidate and not candidate.startswith("/"):
                            clean = re.sub(r"[\r\n\t\x00]", " ", candidate)
                            clean = re.sub(r"\s+", " ", clean).strip()
                            if clean:
                                return clean
                    # Short-circuit: Never fall through to global workspace query if session_id was given
                    return fallback_title

                # Priority 2: Most recent conversation matching workspace (only when session_id not given)
                cursor.execute(
                    "SELECT title, preview, workspace_uris FROM conversation_summaries "
                    "ORDER BY last_modified_time DESC LIMIT 50"
                )
                rows = cursor.fetchall()

                for title, preview, workspace_uris in rows:
                    candidate = (title or "").strip() or (preview or "").strip()
                    if not candidate or candidate.startswith("/"):
                        continue

                    # Strict ZF-024 Sibling Traversal Shield
                    matched = False
                    uris_to_check = []
                    if workspace_uris:
                        try:
                            parsed_uris = json.loads(workspace_uris)
                            if isinstance(parsed_uris, list):
                                uris_to_check = [str(u) for u in parsed_uris]
                        except Exception:
                            pass
                        if not uris_to_check:
                            uris_to_check = re.split(r"[\r\n,]+", str(workspace_uris))

                    for raw_u in uris_to_check:
                        raw_u = raw_u.strip()
                        if not raw_u:
                            continue
                        try:
                            # Normalize scheme prefix if percent-encoded
                            if raw_u.lower().startswith("file%3a"):
                                raw_u = "file:" + raw_u[7:]
                            parsed = urllib.parse.urlparse(raw_u)
                            # Scheme must be file; skip non-file schemes (e.g. vscode-remote://)
                            if parsed.scheme != "file":
                                continue
                            # Drive detection normalization: url2pathname expects ':' for drive detection
                            clean_path = re.sub(r"^/([A-Za-z])%3[Aa]", r"/\1:", parsed.path)
                            path_str = urllib.request.url2pathname(clean_path)
                            p_cand = Path(path_str).resolve()
                            p_norm = os.path.normcase(str(p_cand))

                            # Comparison Direction: workspace must be equal to or inside target_dir (target_path)
                            if p_norm == target_norm or (hasattr(p_cand, "is_relative_to") and p_cand.is_relative_to(target_path)):
                                matched = True
                                break
                        except Exception:
                            continue

                    if matched:
                        # ZF-020: Cleanse whitespaces, CRLF, tabs, null bytes
                        clean = re.sub(r"[\r\n\t\x00]", " ", candidate)
                        clean = re.sub(r"\s+", " ", clean).strip()
                        if clean:
                            return clean
            finally:
                conn.close()
    except Exception as e:
        # Safe single-line diagnostic without leaking sensitive data
        print(f"[ContextFork] Title resolver fallback to repo name ({fallback_title}): {type(e).__name__}", file=sys.stderr)

    return fallback_title


@app.tool()
def get_context_status(workspace_path: str = "") -> str:
    """
    Inspect the current repository Git state and ContextFork checkpoint archive status.
    Returns working tree cleanliness, HEAD commit, branch, and package presence.

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    code, stdout, stderr = _run_cli_command(["status"], target_cwd=target_dir)
    if code != 0:
        return f"❌ Failed to get status for {target_dir}:\n{stderr.strip()}"
    res = f"[Target: {target_dir}]\n{stdout.strip()}"
    if ALLOWED_ROOTS_WARNING:
        res = f"{ALLOWED_ROOTS_WARNING}\n\n" + res
    return res


@app.tool()
def validate_session_context(workspace_path: str = "", package_id: str = "") -> str:
    """
    Validate an existing .contextfork package against the IPSF-1.2 specification.
    Verifies git_diff.patch, metadata integrity, and untracked file SHA-256 hashes.

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
        package_id: Specific snapshot timestamp ID to validate (optional, defaults to latest)
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    args = ["validate"]
    if package_id and package_id.strip():
        args.extend(["--package", re.sub(r"[^\w\-]", "", package_id.strip())])

    code, stdout, stderr = _run_cli_command(args, target_cwd=target_dir)
    if code == 0:
        return f"✅ Validation Succeeded [{target_dir}]:\n{stdout.strip()}"
    return f"❌ Validation Failed [{target_dir}] (Exit code {code}):\n{stdout.strip()}\n{stderr.strip()}"


@app.tool()
def export_session_context(
    workspace_path: str = "",
    session_id: str = "",
    parent_title: str = "",
    goal: str = "Active Development Checkpoint",
    accumulated_tokens: int = 0,
    step_count: int = 0,
    force: bool = False
) -> str:
    """
    Export the current session into a deterministic, verifiable .contextfork checkpoint snapshot.
    Automatically captures git diff HEAD, untracked files, status, lineage title, and IPSF-1.2 metadata.

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
        session_id: Identifier of current parent session (optional)
        parent_title: Title of current conversation for lineage ancestry naming (optional)
        goal: Brief description of the current objective and settled architectural decisions
        accumulated_tokens: Total prompt tokens consumed in this session so far
        step_count: Number of steps executed in this session
        force: Overwrite flag (backward compatible, defaults to False)
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    # Resolve conversation title using layered fallback if not provided or generic
    if parent_title and parent_title.strip() and parent_title.strip().lower() != "session":
        effective_title = parent_title.strip()
    else:
        effective_title = _resolve_conversation_title(target_dir, session_id=session_id)

    args = ["export", "--goal", goal, "--title", effective_title]
    if session_id:
        args.extend(["--session", session_id])
    if accumulated_tokens > 0:
        args.extend(["--tokens", str(accumulated_tokens)])
    if step_count > 0:
        args.extend(["--steps", str(step_count)])
    if force:
        args.append("--force")

    code, stdout, stderr = _run_cli_command(args, target_cwd=target_dir)
    if code != 0:
        return f"❌ Export Failed for [{target_dir}] (Exit code {code}):\n{stderr.strip()}"

    # Resolve latest package and lineage child title from latest.json
    latest_file = target_dir / ".contextfork" / "latest.json"
    child_title = f"{effective_title}-CF-Continuation"
    snap_id = "latest"
    if latest_file.is_file():
        try:
            ldata = json.loads(latest_file.read_text(encoding="utf-8"))
            child_title = ldata.get("child_session_title", child_title)
            snap_id = ldata.get("latest_snapshot_id", snap_id)
        except Exception:
            pass

    # Run quick validation
    v_code, v_stdout, _ = _run_cli_command(["validate"], target_cwd=target_dir)
    val_status = "PASSED" if v_code == 0 else "WARNING/FAILED"

    # Resolve snapshot summary file path for clickable link
    summary_file_path = (target_dir / ".contextfork" / "snapshots" / snap_id / "handoff_summary.md").resolve()
    if summary_file_path.is_file():
        summary_link = f"[{snap_id}](file:///{summary_file_path.as_posix()})"
    else:
        summary_link = f"`{snap_id}`"

    # Extract Git and changes summary from session_metadata.json if available
    meta_file = target_dir / ".contextfork" / "snapshots" / snap_id / "session_metadata.json"
    git_branch = "main"
    git_head = ""
    is_clean = False
    diff_stat = ""
    if meta_file.is_file():
        try:
            mdata = json.loads(meta_file.read_text(encoding="utf-8"))
            gver = mdata.get("git_verification", {})
            git_branch = gver.get("branch", "main")
            git_head = gver.get("head_commit", "")[:7]
            is_clean = gver.get("is_clean", False)
            diff_stat = gver.get("diff_stat", "").strip()
        except Exception:
            pass

    clean_str = "Clean" if is_clean else "Modified"
    diff_block = f"```text\n{diff_stat}\n```" if diff_stat else "*Clean (No changes)*"

    # Verifiable Handoff Card (IPSF-1.2 Lineage Standard - Native Markdown with Clickable Link and Sealed Context)
    handoff_card = (
        f"✅ **ContextFork Checkpoint Successfully Sealed (IPSF-1.2)**\n\n"
        f"### 📦 Sealed Context Summary:\n"
        f"* **📁 Snapshot File:** {summary_link} *(Click to inspect directly)*\n"
        f"* **🏷️ Source Conversation:** `{effective_title}`\n"
        f"* **🎯 Session Goal:** {goal}\n"
        f"* **🌿 Git State:** `{git_branch}` | `{git_head}` ({clean_str})\n"
        f"* **📝 Packaged Changes:**\n{diff_block}\n"
        f"* **🛡️ Verification:** `{val_status}`\n\n"
        f"---\n\n"
        f"### 🔗 New Session Title (Click to copy):\n"
        f"```text\n"
        f"{child_title}\n"
        f"```\n"
        f"*(Copy the title block above using the copy button in the top right corner)*\n\n"
        f"---\n\n"
        f"### 🚀 How to Continue:\n"
        f"1. Click **[ + New Conversation ]** in your AI client (or press `Ctrl+N`).\n"
        f"2. Paste the copied title into your new conversation.\n"
        f"3. In the new conversation, type `/contextfork resume` to restore memory and context."
    )

    return f"{handoff_card}\n\n--- Engine Output ---\n{stdout.strip()}"


def _sanitize_session_id(raw_sid: str) -> tuple[str | None, str | None]:
    """
    Sanitize session_id input:
    Must be <= 128 chars, alphanumeric with '-' and '_', no control characters.
    Returns (cleaned_id, warning_message).
    """
    if not raw_sid or not raw_sid.strip():
        return None, None
    s = raw_sid.strip()
    if len(s) > 128:
        return None, f"session_id exceeds 128 characters ({len(s)} chars), saved as null."
    if not re.match(r"^[A-Za-z0-9_-]+$", s):
        return None, "session_id contains invalid characters, saved as null."
    return s, None


def _atomic_write_metadata(meta_file: Path, mdata: dict) -> None:
    """
    Atomically write metadata to avoid corruption.
    Uses temporary file in the same directory, flush, fsync, close, and os.replace
    with 3x 100ms retry on Windows PermissionError.
    """
    import tempfile
    import time
    parent_dir = meta_file.parent
    serialized = json.dumps(mdata, indent=2).encode("utf-8")

    tmp_path = None
    for attempt in range(3):
        try:
            with tempfile.NamedTemporaryFile(dir=str(parent_dir), delete=False, suffix=".tmp") as tf:
                tmp_path = Path(tf.name)
                tf.write(serialized)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, meta_file)
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.1)
            else:
                raise
        finally:
            if tmp_path and tmp_path.is_file():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass


@app.tool()
def resume_session_context(
    workspace_path: str = "",
    package_id: str = "",
    session_id: str = ""
) -> str:
    """
    Resume active development from an existing .contextfork package in a fresh continuation session.
    Detects Git drift (ZF-021), loads settled decisions, marks package as consumed with UTC timestamp,
    records session_id, and reports archive status.

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
        package_id: Specific snapshot timestamp ID to resume from (optional, defaults to latest)
        session_id: Current continuation conversation session ID for lineage auditing
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    # Resolve target package (unconsumed priority, latest.json fallback disabled for empty unconsumed)
    snapshots_dir = target_dir / ".contextfork" / "snapshots"
    active_pkg_dir = None
    corrupted_packages = []

    if package_id and package_id.strip():
        safe_id = re.sub(r"[^\w\-]", "", package_id.strip())
        candidate = (snapshots_dir / safe_id).resolve()
        if candidate.is_relative_to(snapshots_dir.resolve()) and candidate.is_dir():
            active_pkg_dir = candidate
        else:
            return (
                f"❌ Error: Specified snapshot package '{safe_id}' not found in archive ({snapshots_dir})."
            )
    else:
        # Scan snapshot packages in reverse chronological order using directory name (YYMMDD-HHMMSS)
        unconsumed_snapshots = []
        if snapshots_dir.is_dir():
            snapshot_dirs = sorted([d for d in snapshots_dir.iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
            for s_dir in snapshot_dirs:
                meta_file = s_dir / "session_metadata.json"
                if not meta_file.is_file():
                    corrupted_packages.append(s_dir.name)
                    continue
                try:
                    # Corrupted JSON tolerance
                    mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                    is_consumed = mdata.get("consumed", False)
                    if not is_consumed:
                        unconsumed_snapshots.append(s_dir)
                except Exception as ex:
                    corrupted_packages.append(s_dir.name)
                    print(f"[ContextFork] Corrupted snapshot metadata in {s_dir.name}: {type(ex).__name__}", file=sys.stderr)

        if unconsumed_snapshots:
            active_pkg_dir = unconsumed_snapshots[0]
        else:
            # Check backwards compatibility root if snapshots are empty
            fork_root = target_dir / ".contextfork"
            if not snapshots_dir.is_dir() and (fork_root / "handoff_summary.md").is_file():
                root_meta = fork_root / "session_metadata.json"
                if root_meta.is_file():
                    try:
                        rm_data = json.loads(root_meta.read_text(encoding="utf-8"))
                        if rm_data.get("consumed"):
                            return (
                                f"ℹ️ **No pending unconsumed packages found in archive.**\n\n"
                                f"The legacy root checkpoint package has already been consumed (`consumed: true`).\n"
                            )
                    except Exception:
                        pass
                active_pkg_dir = fork_root
            else:
                # Zero-Pending Guard: If unconsumed == 0 and package_id is empty,
                # DO NOT LOAD ANYTHING! DO NOT TOUCH/OVERWRITE ANY METADATA!
                corrupted_notice = f"\n⚠️  **Unreadable/Corrupted Packages:** `{', '.join(corrupted_packages)}`\n" if corrupted_packages else ""
                details_table = get_package_details(workspace_path=str(target_dir))
                return (
                    f"ℹ️ **No pending unconsumed packages found in archive.**\n\n"
                    f"All checkpoint packages have already been consumed in a session (`consumed: true`).\n"
                    f"To reload a previously consumed package, please explicitly specify `package_id`:\n"
                    f"```text\n/contextfork resume <package_id>\n```\n"
                    f"{corrupted_notice}\n"
                    f"### 📦 Available Packages in Archive:\n\n"
                    f"{details_table}"
                )

    # 1. ZF-021: Git Drift & Phantom State Verification
    meta_file = active_pkg_dir / "session_metadata.json"
    drift_warning = ""
    if meta_file.is_file():
        try:
            mdata = json.loads(meta_file.read_text(encoding="utf-8"))
            pkg_head = mdata.get("git_verification", {}).get("head_commit", "")
            # Current working tree Git HEAD
            cur_check = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"
            )
            if cur_check.returncode == 0:
                cur_head = cur_check.stdout.strip()
                if pkg_head and cur_head and not cur_head.startswith(pkg_head) and not pkg_head.startswith(cur_head):
                    drift_warning = (
                        f"\n⚠️  WARNING (Git Drift): Working tree HEAD has diverged since package snapshot!\n"
                        f"   • Package HEAD:  {pkg_head[:7]}\n"
                        f"   • Current HEAD:  {cur_head[:7]}\n"
                        f"   Please review local working tree changes.\n"
                    )
        except Exception:
            pass

    # 2. Mark package as Consumed
    metadata_warning = ""
    prior_consumed_notice = ""
    session_id_warning = ""
    clean_sid, sid_warn = _sanitize_session_id(session_id)
    if sid_warn:
        session_id_warning = f"\nℹ️ {sid_warn}"

    if active_pkg_dir == target_dir / ".contextfork" and not (active_pkg_dir / "session_metadata.json").is_file():
        # Legacy root package has no metadata file; package could not be marked as 'consumed'
        metadata_warning = "\nℹ️ Legacy root package has no metadata file; package could not be marked as 'consumed'."
    elif meta_file.is_file():
        try:
            mdata = json.loads(meta_file.read_text(encoding="utf-8"))
            if not mdata.get("consumed"):
                mdata["consumed"] = True
                from datetime import datetime, timezone
                mdata["consumed_at"] = datetime.now(timezone.utc).isoformat()
                mdata["consumed_by"] = clean_sid
                _atomic_write_metadata(meta_file, mdata)
            else:
                # Notice for previously consumed package reloaded with explicit package_id
                prior_consumed_notice = (
                    f"\nℹ️ This package has already been consumed.\n"
                    f"   • Consumed at: {mdata.get('consumed_at', 'Unknown')}\n"
                    f"   • Consumed by: {mdata.get('consumed_by', 'null')}\n"
                )
        except Exception as e:
            metadata_warning = f"\n⚠️ Package consumed but failed to update status ({type(e).__name__})"
            print(f"[ContextFork] Metadata write failed in {active_pkg_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)

    # 3. Read handoff_summary.md content
    summary_file = active_pkg_dir / "handoff_summary.md"
    summary_text = summary_file.read_text(encoding="utf-8") if summary_file.is_file() else "No summary available."
    if len(summary_text) > 25000:
        summary_text = summary_text[:25000] + "\n\n... [Summary Truncated for Token Safety]"

    # 4. Snapshot Archive Telemetry Counter
    pkg_count = 0
    consumed_count = 0
    unconsumed_count = 0
    if snapshots_dir.is_dir():
        for s in snapshots_dir.iterdir():
            if s.is_dir() and (s / "session_metadata.json").is_file():
                pkg_count += 1
                try:
                    sm = json.loads((s / "session_metadata.json").read_text(encoding="utf-8"))
                    if sm.get("consumed"):
                        consumed_count += 1
                    else:
                        unconsumed_count += 1
                except Exception:
                    pass

    corrupted_str = f"   • ⚠️ Corrupted: {len(corrupted_packages)} package(s) ({', '.join(corrupted_packages)})\n" if corrupted_packages else ""
    archive_card = (
        f"\n------------------------------------------------------------\n"
        f"📦 ContextFork Archive Status: {consumed_count} consumed, "
        f"{unconsumed_count} pending package(s).\n"
        f"{corrupted_str}"
        f"💡 Tips:\n"
        f"   • For package details: get_package_details (or /contextfork details)\n"
        f"   • To clean archives:   clean_session_context (or /contextfork clean)\n"
        f"------------------------------------------------------------\n"
    )

    return (
        f"🔄 ContextFork Session Memory Successfully Resumed! [Package: {active_pkg_dir.name}]\n"
        f"{drift_warning}"
        f"{metadata_warning}"
        f"{prior_consumed_notice}"
        f"{session_id_warning}\n"
        f"{summary_text}\n"
        f"{archive_card}\n"
        f"> [!NOTE]\n"
        f"> Context successfully synchronized. You may now continue your work with a fresh context."
    )


@app.tool()
def get_package_details(workspace_path: str = "") -> str:
    """
    List all archived snapshots in the .contextfork repository with their consumed state and metadata.
    Returns a structured Markdown table (ZF-010 Token Safe).

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    code, stdout, stderr = _run_cli_command(["details"], target_cwd=target_dir)
    if code != 0:
        return f"❌ Failed to get package details for {target_dir}:\n{stderr.strip()}"
    return stdout.strip()


@app.tool()
def clean_session_context(
    workspace_path: str = "",
    confirm: bool = False,
    all_packages: bool = False
) -> str:
    """
    Clean old or consumed snapshot packages from the .contextfork archive.
    Requires explicit confirm=True to delete (Dry-run by default for safety).

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
        confirm: Must be explicitly set to True to physically delete files (default: False dry-run)
        all_packages: If True, also cleans unconsumed packages (default: False, preserves unconsumed)
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    args = ["clean"]
    if confirm:
        args.append("--confirm")
    if all_packages:
        args.append("--all")

    code, stdout, stderr = _run_cli_command(args, target_cwd=target_dir)
    if code != 0:
        return f"❌ Clean command failed for {target_dir}:\n{stderr.strip()}"

    if not confirm:
        # Dry-run: Extract eligible count and render single red button Tailwind card
        snapshots_dir = target_dir / ".contextfork" / "snapshots"
        consumed_count = 0
        if snapshots_dir.is_dir():
            for s in snapshots_dir.iterdir():
                if s.is_dir() and (s / "session_metadata.json").is_file():
                    try:
                        sm = json.loads((s / "session_metadata.json").read_text(encoding="utf-8"))
                        if sm.get("consumed") or all_packages:
                            consumed_count += 1
                    except Exception:
                        pass

        if consumed_count == 0:
            return (
                "✨ ContextFork archive is clean. No consumed snapshot packages to remove.\n\n"
                f"{stdout.strip()}"
            )

        safe_count = int(consumed_count)
        clean_card = (
            f"🧹 **ContextFork Cleanup (Dry-Run)**\n\n"
            f"* **Eligible for removal:** `{safe_count}` consumed snapshot package(s).\n"
            f"* **Action:** To confirm physical deletion, invoke `clean_session_context(confirm=True)` or run:\n"
            f"```text\n/contextfork clean confirm\n```\n\n"
            f"> [!TIP]\n"
            f"> To keep your archive intact, simply ignore this notice and continue your work."
        )
        return f"{clean_card}\n\n--- Status ---\n{stdout.strip()}"

    return stdout.strip()


@app.tool()
def read_handoff_summary(workspace_path: str = "", package_id: str = "") -> str:
    """
    Read the handoff_summary.md file from an active or specified package in .contextfork.
    Read-only inspection: does not modify metadata.

    Args:
        workspace_path: Absolute path to the git project (optional, defaults to ContextFork repo)
        package_id: Specific snapshot timestamp ID to read (optional, defaults to latest unconsumed)
    """
    try:
        target_dir = _validate_and_resolve_workspace(workspace_path)
    except (PermissionError, ValueError) as err:
        return f"❌ Security/Path Error: {err}"

    snapshots_dir = target_dir / ".contextfork" / "snapshots"
    summary_file = None

    if package_id and package_id.strip():
        safe_id = re.sub(r"[^\w\-]", "", package_id.strip())
        pkg_dir = (snapshots_dir / safe_id).resolve()
        if snapshots_dir.is_dir() and pkg_dir.is_relative_to(snapshots_dir.resolve()) and pkg_dir.is_dir():
            candidate = pkg_dir / "handoff_summary.md"
            if candidate.is_file():
                summary_file = candidate
            else:
                return f"❌ Snapshot package '{safe_id}' does not contain handoff_summary.md."
        else:
            return f"❌ Snapshot package '{safe_id}' not found in archive ({snapshots_dir})."
    elif snapshots_dir.is_dir():
        # Select latest unconsumed package; if none pending, return notice without dumping summary
        unconsumed_snapshots = []
        snapshot_dirs = sorted([d for d in snapshots_dir.iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
        for s_dir in snapshot_dirs:
            meta_file = s_dir / "session_metadata.json"
            if meta_file.is_file():
                try:
                    mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                    if not mdata.get("consumed", False):
                        unconsumed_snapshots.append(s_dir)
                except Exception:
                    pass

        if unconsumed_snapshots:
            summary_file = unconsumed_snapshots[0] / "handoff_summary.md"
        else:
            all_pkgs = sorted([p.name for p in snapshots_dir.iterdir() if p.is_dir()], reverse=True)
            last_id = all_pkgs[0] if all_pkgs else "None"
            return (
                f"ℹ️ **No pending unconsumed handoff package found.**\n\n"
                f"Latest archived package: `{last_id}`\n"
                f"To inspect a specific package, use: `read_handoff_summary(package_id='{last_id}')`"
            )
    else:
        root_summary = target_dir / ".contextfork" / "handoff_summary.md"
        if root_summary.is_file():
            summary_file = root_summary

    if not summary_file or not summary_file.is_file():
        return (
            f"❌ No handoff_summary.md found in {target_dir}.\n"
            "Run 'export_session_context' first to generate a package."
        )

    content = summary_file.read_text(encoding="utf-8")
    if len(content) > 30000:
        return content[:30000] + "\n\n... [Truncated for Token Safety: View file directly for full text]"
    return content


if __name__ == "__main__":
    app.run(transport="stdio")
