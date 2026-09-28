# Security Policy

## Supported Versions

The following versions of **ContextFork (IPSF)** are actively maintained and supported with security updates:

| Version | Supported |
| :--- | :--- |
| 1.2.x (IPSF-1.2) | :white_check_mark: |
| < 1.2.0 | :x: |

---

## Security Model & Zero-Fault (ZF) Defense Architecture

ContextFork implements a defense-in-depth security model to ensure autonomous coding agents and MCP clients cannot be coerced into unauthorized filesystem access, credential exfiltration, or context poisoning.

### Core Security Contracts

1. **ZF-026 Allowed Roots Contract (Fail-Closed):**
   * Configured via the `CONTEXTFORK_ALLOWED_ROOTS` environment variable (semicolon-delimited list of absolute paths).
   * **Fail-Closed by Design:** If *any* configured root violates security constraints (relative path, directory traversal `..`, UNC network path, junction/symlink escape, drive root `C:\`, or user home directory), the entire configuration is rejected. The server either safely falls back to the local repository root (if verified as a Git repo) or runs with an empty allowed roots list.
   * Access to any path outside the verified allowed roots triggers an immediate `PermissionError`.

2. **ZF-024 Cross-Host Context Isolation:**
   * ContextFork prevents cross-client context leakage. Local IDE database queries (e.g. conversation summaries) are strictly isolated and never accessed unless an explicit host signal (`CONTEXTFORK_HOST="antigravity"`) is provided.
   * For clients like Claude Desktop and Cursor, title resolution defaults safely to the local repository directory name.

3. **ZF-021 Git Drift & Phantom State Verification:**
   * Context resumption (`resume_session_context`) deterministically cross-checks the snapshot's recorded Git HEAD SHA against the current working tree HEAD. Divergence triggers prominent drift warnings to prevent applying stale decisions to altered codebases.

4. **ZF-020 Input Sanitization & Lineage Provenance:**
   * All conversation titles, session IDs, and snapshot IDs are cleansed of control characters (`\r`, `\n`, `\t`, `\0`).
   * Session IDs are constrained to 128 alphanumeric characters (`[A-Za-z0-9_-]`). Malformed or oversized IDs are sanitized to `null` to prevent SQL/JSON injection.

5. **ZF-022 Markdown Sanitization Shield:**
   * Interactive agent skills avoid raw HTML execution elements (`<button onclick="...">`). All agent interactions rely strictly on native client modals (`ask_question`) or standard Markdown links.

6. **ZF-008 Atomic State Updates:**
   * Metadata files are written via atomic temporary files with explicit `flush`, `os.fsync`, and atomic replacement (`os.replace`) with exponential backoff retry to prevent state corruption during concurrent access.

---

## Reporting a Vulnerability

We take the security of ContextFork seriously. If you discover a vulnerability or potential security risk:

1. **Do NOT report security vulnerabilities via public GitHub issues.**
2. Report vulnerabilities confidentially through [GitHub Privately Reported Security Advisories](https://github.com/mrblackman/ContextFork/security/advisories/new) or contact the project maintainer via GitHub ([@mrblackman](https://github.com/mrblackman)).
3. Please include:
   * Description of the vulnerability.
   * Steps to reproduce or proof-of-concept script.
   * Potential impact and affected components (CLI, MCP Server, or Skill).

We appreciate responsible disclosure and will respond promptly to review and resolve reported issues.
