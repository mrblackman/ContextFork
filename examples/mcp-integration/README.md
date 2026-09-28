# ContextFork — Model Context Protocol (MCP) Integration Guide
### Native In-Place Session Forking via MCP Tools (IPSF-1.2)

This example demonstrates how to integrate **ContextFork (IPSF-1.2)** into any AI agent environment compatible with the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) — including Claude Desktop, Cursor, Antigravity, Cline, and Roo-Code.

---

## 🎯 What This Enables

When an autonomous agent session reaches high step counts or token horizons (>60k–100k tokens), attention dilution degrades reasoning accuracy ("Context Rot").

By exposing ContextFork as an MCP server, the agent can:
1. **Monitor Its Own Footprint:** Query working tree health and active fork state.
2. **Autonomously Fork Session:** Package architectural decisions, uncommitted working tree changes, and anti-loop warnings into a machine-verifiable `.contextfork/` bundle before handing off to a clean session.
3. **Instantly Ingest Past State:** Child continuation sessions can read `handoff_summary.md` and verify ground truth with zero context loss.

---

## 🛠️ Provided MCP Tools (7 Core Tools)

| Tool Name | Type | Description |
|---|---|---|
| `get_context_status()` | Read-only | Returns Git repository state (HEAD, branch, dirty tree) and presence of a `.contextfork` package. |
| `export_session_context(...)` | Mutation | Generates the deterministic `.contextfork/` package and immediately validates it against IPSF-1.2. |
| `validate_session_context(...)` | Read-only | Validates package integrity, diff applicability, and SHA-256 hashes of untracked files. |
| `resume_session_context(...)` | Mutation | Resumes active development, verifies Git drift, loads past decisions, and marks package as consumed with UTC timestamp and session ID. |
| `get_package_details(...)` | Read-only | Returns structured Markdown table of all archived snapshots with consumed state and commit hashes. |
| `clean_session_context(...)` | Mutation | Cleans old consumed snapshot packages with dry-run safety and explicit confirmation. |
| `read_handoff_summary(...)` | Read-only | Reads `handoff_summary.md` from active or specified snapshot package with zero-pending safety. |

---

## 📦 Prerequisites & Installation

ContextFork's core engine (`contextfork.py`) requires **zero external dependencies** (pure Python standard library). This MCP wrapper only requires the official Python MCP SDK:

```bash
pip install mcp
```

*(Compatible with both `mcp` 1.x and `mcp` 2.x standards).*

---

## ⚙️ Client Configurations

### 1. Claude Desktop
Add to your `claude_desktop_config.json` (`%APPDATA%\Claude\claude_desktop_config.json` on Windows or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "contextfork": {
      "command": "python",
      "args": [
        "/absolute/path/to/ContextFork/examples/mcp-integration/contextfork_mcp.py"
      ]
    }
  }
}
```

### 2. Cursor (Features → MCP)
In Cursor Settings:
* **Name:** `contextfork`
* **Type:** `command`
* **Command:** `python /absolute/path/to/ContextFork/examples/mcp-integration/contextfork_mcp.py`

### 3. Antigravity MCP Server Configuration
In your Antigravity user or project configuration (`mcp_config.json`):

```json
{
  "mcpServers": {
    "contextfork": {
      "command": "python",
      "args": [
        "/absolute/path/to/ContextFork/examples/mcp-integration/contextfork_mcp.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "CONTEXTFORK_HOST": "antigravity",
        "CONTEXTFORK_ALLOWED_ROOTS": "/absolute/path/to/my-first-repo;/absolute/path/to/ContextFork"
      }
    }
  }
}
```

> **Note on Host Isolation (ZF-024):** Set `CONTEXTFORK_HOST: "antigravity"` when running under Antigravity so the MCP server can resolve conversation titles from Antigravity's local SQLite summaries. For Claude Desktop or Cursor, omit this variable to enforce strict host isolation and fallback safely to the repository root name.
>
> **Note on Permitted Roots (ZF-026):** Specify permitted project workspace directories using `CONTEXTFORK_ALLOWED_ROOTS` (separated by `;`). Paths must be absolute directories without traversal or UNC paths. Invalid paths trigger Fail-Closed protection.

---

## 🔄 End-to-End Workflow: How an Agent Uses It

### Phase 1: Parent Session Reaches Threshold
When the agent has executed 50+ steps or notes critical architectural decisions, it calls:

```json
{
  "name": "export_session_context",
  "arguments": {
    "session_id": "session-backend-refactor-92",
    "goal": "Migrate authentication to JWT and add refresh token rotation",
    "accumulated_tokens": 84500,
    "step_count": 92,
    "force": true
  }
}
```

The tool generates:
- `.contextfork/git_diff.patch` (all uncommitted changes, CRLF + binary safe)
- `.contextfork/git_status.json` (machine git snapshot)
- `.contextfork/untracked/` + `untracked_manifest.json` (captured new files with SHA-256)
- `.contextfork/session_metadata.json` (IPSF-1.2 manifest)
- `.contextfork/handoff_summary.md` (6-part structured template)

### Phase 2: Child Continuation Session
The developer starts a fresh session in the same workspace. The new agent simply runs:

```json
{
  "name": "read_handoff_summary"
}
```

And verifies the ground truth:

```json
{
  "name": "validate_session_context"
}
```

The continuation session immediately proceeds with 100% attention capacity, zero hallucinated history, and verified working tree integrity.
