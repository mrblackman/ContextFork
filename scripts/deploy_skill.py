#!/usr/bin/env python3
"""
ContextFork Antigravity Deployment Automation Example (IPSF-1.2 Reference Script)

NOTE: This script is an environment-specific deployment example tailored for Google Antigravity
(and its internal schema cache / FastMCP manager). It demonstrates the isolated profile
deployment topology (Option B) with SHA-256 target integrity and tier guards.
Universal, cross-client deployment automation for other MCP clients (Claude Desktop, Cursor, etc.)
is on the project roadmap (v1.3).

Security Contract:
1. Profiles with tier='live' CANNOT be deployed directly via --profile.
2. The ONLY path to tier='live' is through the interactive --promote gate.
3. Pre-deploy integrity check: detects manual modifications at target.
4. Post-deploy verification: verifies SHA-256 hashes of deployed files.
5. Content-addressed dirty directory naming: bin/<commit>-dirty-<content-hash>/
6. Python executable path is loaded dynamically from profiles.json (zero drive literals).
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
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples" / "mcp-integration"))

import contextfork_mcp

PROFILES_CONFIG_PATH = REPO_ROOT / "scripts" / "profiles.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_git_info(target_repo: Path = REPO_ROOT) -> tuple[str, str, bool]:
    """Returns (full_commit_hash, short_commit_hash, is_dirty)."""
    try:
        r_head = subprocess.run(
            ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        full_hash = r_head.stdout.strip()
        short_hash = full_hash[:7]

        r_status = subprocess.run(
            ["git", "-C", str(target_repo), "status", "--porcelain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        is_dirty = bool(r_status.stdout.strip())
        return full_hash, short_hash, is_dirty
    except Exception as e:
        print(f"❌ Git error: {e}", file=sys.stderr)
        sys.exit(1)


def load_profiles(config_path: Path = PROFILES_CONFIG_PATH) -> dict:
    if not config_path.is_file():
        example_path = REPO_ROOT / "scripts" / "profiles.example.json"
        print(
            f"❌ profiles.json not found at {config_path}.\n"
            f"   Please copy {example_path} to {config_path} and configure your local environment profiles.",
            file=sys.stderr
        )
        sys.exit(1)
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))["profiles"]
    except Exception as e:
        print(f"❌ Failed to parse profiles.json: {e}", file=sys.stderr)
        sys.exit(1)


def atomic_write(target_path: Path, data: str | bytes) -> None:
    parent = target_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    encoding = None if isinstance(data, bytes) else "utf-8"

    tmp = tempfile.NamedTemporaryFile(dir=str(parent), delete=False, mode=mode, encoding=encoding, suffix=".tmp")
    try:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, target_path)
    finally:
        if os.path.exists(tmp.name):
            try:
                os.unlink(tmp.name)
            except Exception:
                pass


def update_mcp_config(config_path: Path, bin_dir: Path, profile_cfg: dict, dry_run: bool = False) -> None:
    """Updates target mcp_config.json safely with timestamped backup."""
    if not config_path.parent.is_dir():
        print(f"❌ Config parent directory does not exist: {config_path.parent}", file=sys.stderr)
        sys.exit(1)

    existing_data = {"mcpServers": {}}
    if config_path.is_file():
        content = config_path.read_text(encoding="utf-8").strip()
        if content:
            try:
                existing_data = json.loads(content)
            except Exception as e:
                print(f"❌ Target mcp_config.json is corrupted and non-empty. Refusing to overwrite: {e}", file=sys.stderr)
                sys.exit(1)

    # Validate allowed roots with ZF-026 before writing
    roots_str = ";".join(profile_cfg.get("allowed_roots", []))
    validated_roots, warn = contextfork_mcp.validate_and_parse_allowed_roots(env_str=roots_str)
    if warn:
        print(f"❌ ZF-026 Validation failed for profile allowed_roots: {warn}", file=sys.stderr)
        sys.exit(1)

    mcp_script = bin_dir / "contextfork_mcp.py"

    # Merge contextfork server entry
    if "mcpServers" not in existing_data or not isinstance(existing_data["mcpServers"], dict):
        existing_data["mcpServers"] = {}

    # Load python_path from profile_cfg dynamically (zero hardcoded drive literals)
    py_exec = profile_cfg.get("python_path", sys.executable)

    existing_data["mcpServers"]["contextfork"] = {
        "command": py_exec,
        "args": [
            str(mcp_script.as_posix())
        ],
        "env": {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(bin_dir.as_posix()),
            "CONTEXTFORK_HOST": profile_cfg.get("host", "antigravity"),
            "CONTEXTFORK_ALLOWED_ROOTS": roots_str
        }
    }

    serialized = json.dumps(existing_data, indent=2)

    if dry_run:
        print(f"🔍 [Dry-Run] Would update {config_path}:\n{serialized}\n")
        return

    # Create timestamped backup if file exists and has content
    if config_path.is_file() and config_path.stat().st_size > 0:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak_file = config_path.parent / f"{config_path.name}.{ts}.bak"
        shutil.copy2(config_path, bak_file)
        print(f"📦 Backup created: {bak_file}")

    atomic_write(config_path, serialized)
    print(f"✅ Updated {config_path}")


def deploy_profile(
    profile_name: str,
    profile_cfg: dict,
    full_hash: str,
    short_hash: str,
    is_dirty: bool,
    dry_run: bool = False,
    is_promoting: bool = False,
    force_overwrite: bool = False,
    repo_root: Path = REPO_ROOT
) -> None:
    tier = profile_cfg.get("tier", "sandbox")
    # Tier Isolation Guard: Tier 'live' profiles CANNOT be deployed directly
    if tier == "live" and not is_promoting:
        print(
            f"❌ Security Violation (Tier Isolation Guard): Profile '{profile_name}' is tier 'live'.\n"
            f"   Direct deployment via --profile is strictly blocked! You must use --promote.",
            file=sys.stderr
        )
        sys.exit(1)

    print(f"\n🚀 Deploying ContextFork to profile: [{profile_name}] (Tier: {tier})")
    print(f"   • Commit: {short_hash} {'(DIRTY)' if is_dirty else '(CLEAN)'}")

    config_path = Path(profile_cfg["config_path"])
    skill_path = Path(profile_cfg["skill_path"])

    # Determine user home deterministically from config_path (<user_home>/.gemini/antigravity/mcp_config.json)
    if len(config_path.parents) >= 3:
        dest_user_home = config_path.parents[2]
    else:
        dest_user_home = Path(os.environ.get("USERPROFILE", Path.home()))

    # Source files
    src_mcp = repo_root / "examples" / "mcp-integration" / "contextfork_mcp.py"
    src_cli = repo_root / "contextfork.py"
    src_skill = repo_root / ".agents" / "skills" / "contextfork" / "SKILL.md"

    for src in [src_mcp, src_cli, src_skill]:
        if not src.is_file():
            print(f"❌ Missing source file: {src}", file=sys.stderr)
            sys.exit(1)

    hashes = {
        "contextfork_mcp.py": sha256_file(src_mcp),
        "contextfork.py": sha256_file(src_cli),
        "SKILL.md": sha256_file(src_skill),
    }

    # Content-addressed dirty naming
    content_hash = hashlib.sha256("".join(sorted(hashes.values())).encode("utf-8")).hexdigest()[:6]
    version_suffix = f"{short_hash}" if not is_dirty else f"{short_hash}-dirty-{content_hash}"
    dest_bin_dir = dest_user_home / ".contextfork" / "bin" / version_suffix

    # Pre-deployment target integrity check
    # Check if target manifest exists and verify if target files were modified manually
    target_manifest_path = dest_user_home / ".contextfork" / "deploy_manifest.json"
    if target_manifest_path.is_file() and not force_overwrite:
        try:
            old_manifest = json.loads(target_manifest_path.read_text(encoding="utf-8"))
            old_files = old_manifest.get("files", {})
            # Verify SKILL.md
            if skill_path.is_file():
                cur_skill_hash = sha256_file(skill_path)
                expected_skill_hash = old_files.get("SKILL.md")
                if expected_skill_hash and cur_skill_hash != expected_skill_hash and cur_skill_hash != hashes["SKILL.md"]:
                    print(
                        f"❌ Target Integrity Violation: File at {skill_path} was modified manually!\n"
                        f"   Target Hash:   {cur_skill_hash[:12]}...\n"
                        f"   Manifest Hash: {expected_skill_hash[:12]}...\n"
                        f"   Aborting deployment to prevent overwriting manual changes. Use --force-overwrite to bypass.",
                        file=sys.stderr
                    )
                    sys.exit(1)
        except json.JSONDecodeError:
            pass

    manifest = {
        "manifest_version": "1.0",
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "profile": profile_name,
        "commit_hash": full_hash,
        "short_commit": short_hash,
        "dirty": is_dirty,
        "content_hash": content_hash,
        "files": hashes,
        "paths": {
            "bin_dir": str(dest_bin_dir),
            "config_path": str(config_path),
            "skill_path": str(skill_path),
        }
    }

    if dry_run:
        print(f"🔍 [Dry-Run] Would copy files to: {dest_bin_dir}")
        for fname, h in hashes.items():
            print(f"   • {fname} (SHA256: {h[:12]}...)")
        print(f"🔍 [Dry-Run] Would copy SKILL.md to: {skill_path}")
        print(f"🔍 [Dry-Run] Would write deploy_manifest.json")
        update_mcp_config(config_path, dest_bin_dir, profile_cfg, dry_run=True)
        print("🔍 [Dry-Run] Completed simulation without changes.")
        return

    # Physical write
    dest_bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_mcp, dest_bin_dir / "contextfork_mcp.py")
    shutil.copy2(src_cli, dest_bin_dir / "contextfork.py")
    shutil.copy2(src_skill, dest_bin_dir / "SKILL.md")

    # Deploy skill to actual Antigravity skill path
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_skill, skill_path)
    print(f"✅ Deployed SKILL.md -> {skill_path}")

    # Post-deployment verification of target hashes
    for fname, expected_hash in hashes.items():
        actual_hash = sha256_file(dest_bin_dir / fname)
        if actual_hash != expected_hash:
            print(f"❌ Post-deploy hash mismatch for {fname}: expected {expected_hash}, got {actual_hash}", file=sys.stderr)
            sys.exit(1)

    skill_target_hash = sha256_file(skill_path)
    if skill_target_hash != hashes["SKILL.md"]:
        print(f"❌ Post-deploy hash mismatch for {skill_path}", file=sys.stderr)
        sys.exit(1)

    print("✅ Target file SHA-256 hashes successfully verified.")

    # Write manifests
    manifest_data = json.dumps(manifest, indent=2)
    atomic_write(dest_bin_dir / "deploy_manifest.json", manifest_data)
    atomic_write(dest_user_home / ".contextfork" / "deploy_manifest.json", manifest_data)
    print(f"✅ Written deploy_manifest.json")

    # Deploy FastMCP tool schemas to Antigravity tool cache
    deploy_mcp_schemas(dest_user_home, dry_run=False)

    # Update mcp_config.json
    update_mcp_config(config_path, dest_bin_dir, profile_cfg, dry_run=False)
    print(f"🎉 Successfully deployed to [{profile_name}]!\n")


def deploy_mcp_schemas(dest_user_home: Path, dry_run: bool = False) -> None:
    schema_dir = dest_user_home / ".gemini" / "antigravity" / "mcp" / "contextfork"
    if not schema_dir.parent.is_dir():
        return
    schema_dir.mkdir(parents=True, exist_ok=True)
    for tool in contextfork_mcp.app._tool_manager.list_tools():
        schema_data = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        target_json = schema_dir / f"{tool.name}.json"
        if dry_run:
            print(f"🔍 [Dry-Run] Would update MCP schema {target_json}")
        else:
            atomic_write(target_json, json.dumps(schema_data, indent=2))


def main():
    parser = argparse.ArgumentParser(description="ContextFork Deployment Automation")
    parser.add_argument("--sandbox", action="store_true", help="Deploy to sandbox (User) profile")
    parser.add_argument("--promote", action="store_true", help="Promote verified sandbox version to live profile")
    parser.add_argument("--dry-run", action="store_true", help="Simulate deployment without modifying any files")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow deploying from uncommitted/dirty git state")
    parser.add_argument("--force-overwrite", action="store_true", help="Bypass manual modification check on target")
    parser.add_argument("--profile", type=str, help="Explicit profile name from profiles.json (sandbox tier only)")

    args = parser.parse_args()

    full_hash, short_hash, is_dirty = get_git_info()

    if is_dirty and not args.allow_dirty:
        print("❌ Working tree is dirty (uncommitted changes exist).\n"
              "   Commit changes first or use --allow-dirty for test deployments.", file=sys.stderr)
        sys.exit(1)

    profiles = load_profiles()

    if args.promote:
        print("🔒 PROMOTION GATE: Promoting to live developer profile.")
        print(f"   ℹ️ Current OS user: {os.environ.get('USERNAME', 'unknown')}. Promotion should run in target profile session.")

        user_cfg = profiles.get("User")
        if not user_cfg:
            print("❌ User profile not found in profiles.json", file=sys.stderr)
            sys.exit(1)

        user_cfg_path = Path(user_cfg["config_path"])
        user_home = user_cfg_path.parents[2] if len(user_cfg_path.parents) >= 3 else Path.home()
        user_manifest_file = user_home / ".contextfork" / "deploy_manifest.json"
        if not user_manifest_file.is_file():
            print(f"❌ User sandbox manifest not found at {user_manifest_file}.\n"
                  f"   You must deploy and test on User profile (--sandbox) before promoting!", file=sys.stderr)
            sys.exit(1)

        user_manifest = json.loads(user_manifest_file.read_text(encoding="utf-8"))
        if user_manifest.get("dirty"):
            print("❌ User sandbox manifest is marked as DIRTY. Cannot promote untested dirty build!", file=sys.stderr)
            sys.exit(1)

        tested_commit = user_manifest.get("short_commit")
        if tested_commit != short_hash:
            print(f"❌ Repo HEAD ({short_hash}) does not match tested sandbox commit ({tested_commit}).\n"
                  f"   Please test current HEAD in sandbox first!", file=sys.stderr)
            sys.exit(1)

        # Enforce interactive TTY (Ban autonomous promotion)
        if not sys.stdin.isatty():
            print("❌ Autonomous promotion banned: --promote requires an interactive terminal (TTY)!", file=sys.stderr)
            sys.exit(1)

        live_candidates = [name for name, p in profiles.items() if p.get("tier") == "live"]
        if not live_candidates:
            print("❌ Promotion failed: No profile with tier 'live' found in profiles configuration.", file=sys.stderr)
            sys.exit(1)
        elif len(live_candidates) > 1:
            print(f"❌ Promotion failed: Multiple profiles with tier 'live' found ({live_candidates}). "
                  f"Profiles configuration must define exactly one 'live' tier profile.", file=sys.stderr)
            sys.exit(1)
        target_live_name = live_candidates[0]

        print(f"\n📋 Verified Commit: {tested_commit}")
        print(f"   Target: live developer profile '{target_live_name}'")
        confirm = input(f"👉 To confirm promotion, type '{tested_commit}': ").strip()
        if confirm != tested_commit:
            print("❌ Confirmation mismatch. Promotion aborted.", file=sys.stderr)
            sys.exit(1)

        deploy_profile(
            target_live_name,
            profiles[target_live_name],
            full_hash,
            short_hash,
            is_dirty,
            dry_run=args.dry_run,
            is_promoting=True,
            force_overwrite=args.force_overwrite
        )

    elif args.sandbox:
        if "User" not in profiles:
            print("❌ Profile 'User' not found in profiles.json", file=sys.stderr)
            sys.exit(1)
        deploy_profile(
            "User",
            profiles["User"],
            full_hash,
            short_hash,
            is_dirty,
            dry_run=args.dry_run,
            is_promoting=False,
            force_overwrite=args.force_overwrite
        )

    elif args.profile:
        if args.profile not in profiles:
            print(f"❌ Profile '{args.profile}' not found in profiles.json", file=sys.stderr)
            sys.exit(1)

        target_cfg = profiles[args.profile]
        # Tier Isolation Guard: Block live tier profiles via --profile
        if target_cfg.get("tier") == "live":
            print(
                f"❌ Security Violation (Tier Isolation Guard): Profile '{args.profile}' has tier 'live'.\n"
                f"   Deploying to live profiles via --profile is prohibited! Use --promote.",
                file=sys.stderr
            )
            sys.exit(1)

        deploy_profile(
            args.profile,
            target_cfg,
            full_hash,
            short_hash,
            is_dirty,
            dry_run=args.dry_run,
            is_promoting=False,
            force_overwrite=args.force_overwrite
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
