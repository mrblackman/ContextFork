# ContextFork — In-Place Session Forking Protocol (IPSF-1.2)
### A Verifiable, Vendor-Neutral Handoff Specification for Long-Horizon Agentic LLM Sessions

> **IPSF** — *In-Place Session Forking Protocol*: a vendor-neutral open specification for deterministic, verifiable context handoff between AI agent sessions.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Specification: IPSF-1.2](https://img.shields.io/badge/Specification-IPSF--1.2-brightgreen.svg)](schemas/)
[![Reference Implementation: Python](https://img.shields.io/badge/Reference%20CLI-contextfork.py-blueviolet.svg)](contextfork.py)
[![Target: AI Coding Agents](https://img.shields.io/badge/Target-AI%20Coding%20Agents-orange.svg)](https://github.com/mrblackman/ContextFork)

**Author:** Mustafa KILINC ([@mrblackman](https://github.com/mrblackman))  
**Version:** IPSF-1.2 (Normative Protocol Specification)  
**Document ID:** `RFC-IPSF-001`  
**Repository:** [github.com/mrblackman/ContextFork](https://github.com/mrblackman/ContextFork)  

---

> 💡 **Core Principles:**  
> *"LLM summarizes; machines verify."*  
> *"Don't ask the AI to remember what the machine can verify."*

> ⚠️ **Specification Status: RFC Draft — Under Active Development**  
> This repository defines an open, vendor-neutral protocol. The accompanying `contextfork.py` is a **reference implementation** that demonstrates the method is buildable — it is not a production-ready tool. We welcome architectural feedback, peer review, and independent implementations from the autonomous agent engineering community.

---

## 🎯 1. Executive Summary

Modern Large Language Models advertise theoretical context windows of 1M to 2M+ tokens. However, empirical research (Stanford's *Lost in the Middle*, Chroma's *Maximum Effective Context Window*, RULER) establishes that **effective reasoning accuracy for autonomous agentic coding degrades significantly as token contexts expand ("Context Rot")**.

In extended engineering sessions (100–250+ steps), developers face two compounding bottlenecks:
1. **Zero Context Observability:** Users operate without real-time indicators for accumulated prompt tokens, tool output weight, or step counts. Sessions silently balloon past 100,000+ tokens, turning every routine prompt into a massive compute drain and triggering severe attention dilution.
2. **The "New Chat" Abstraction Failure:** The traditional "New Chat" button is a destructive reset. Developers resist starting fresh sessions because manually summarizing 50+ steps of architectural decisions, file modifications, and pending tasks creates severe handoff friction.

**ContextFork (IPSF-1.2)** formalizes an interoperable, vendor-agnostic protocol solving this dilemma through two core pillars:
1. **Ambient Context Telemetry:** Real-time UI visibility into active token count, step count, and attention degradation risk signals.
2. **Native "Summarize & Fork" (Session Forking):** A structured handoff protocol that compresses a bloated session into a **verifiable 6-part handoff package**, enabling a clean continuation session in the same workspace with preserved architectural decisions and machine-verifiable working state.

---

## 💥 2. The Problem: "Context Rot" as an Attention Risk Signal

In transformer architectures, attention weights dilute over large token horizons. As prompt context grows:
* **Attention Dilution:** The attention matrix dilutes across historical terminal noise, failed tool attempts, and verbose build logs.
* **Repetitive Failure Looping:** Models begin treating their own past failed tool calls as "ground truth" or stylistic guidelines, falling into repetitive failure loops.
* **Loss of System Constraints:** High-priority system instructions (Constitutional rules, security isolation, git practices) placed at the beginning of the context fall into the "Lost in the Middle" trough.

> **Context Compression ≠ Context Preservation:**  
> Compressing 120,000 tokens into 2,000 tokens is inherently a lossy compression. If an agent summary omits *why* certain paths failed or lacks machine-verifiable evidence, the newly spawned child agent will repeat identical mistakes. True continuity requires pairing high-level LLM synthesis with deterministic working-tree state.

### Motivating Example
During an active engineering session on an AI coding agent platform:
* **Step Count:** 226 steps
* **Transcript Size on Disk:** 408 KB
* **Accumulated Tokens:** ~119,400 tokens

At this scale, every user reply forces the model to re-ingest the entire history before outputting a response, burning quota, spiking Time To First Token (TTFT), and multiplying attention dilution risks. This is the concrete problem IPSF addresses.

---

## 📊 3. Feature 1: Real-Time Context Observability & Telemetry

AI coding environments SHOULD provide ambient, live feedback on the conversation's physical footprint.

### 3.1. UI Placement & Anatomy
Located on the status bar (adjacent to the model selector) or directly under assistant turns:

```text
[ 🟢 34.2k tokens | Step 28 | Normal ]
[ 🟡 68.5k tokens | Step 84 | Caution ]
[ 🔴 119.4k tokens | Step 226 | Critical — Fork Recommended ]
```

### 3.2. Detailed Telemetry Inspector (Pop-over)
Clicking the badge exposes a diagnostic breakdown complying with [`context-telemetry.schema.json`](schemas/context-telemetry.schema.json):

```text
CONTEXT METRICS
────────────────────────────────────────────
Total Active Tokens:   119,400
├── Input / History:    78,200
├── Tool Outputs:       32,400
└── System Prompt:       8,800
Steps Executed:        226
Session Age:           2h 45m
Touched Files:         18 files
Failed Shell Commands: 4 (Handled)

RECOMMENDATION:
⚠️ Attention Dilution Risk Elevated
✓ Recommendation: Trigger "Summarize & Fork"
```

### 3.3. Configurable Policy & Reference Thresholds
Rather than hardcoded limits, ContextFork specifies configurable heuristic defaults via [`context-policy.schema.json`](schemas/context-policy.schema.json). The following are **reference defaults only** — implementors SHOULD adjust these to their model and task characteristics:

```json
{
  "context_policy": {
    "tokens": {
      "warning_threshold": 50000,
      "fork_recommended_threshold": 80000,
      "hard_limit": null
    },
    "steps": {
      "warning_threshold": 75,
      "fork_recommended_threshold": 120
    }
  }
}
```

| Token Range | Step Range | Risk Level | Indicator | Recommended Action |
| :--- | :--- | :--- | :--- | :--- |
| `< 50,000` | `< 75` | **Normal** | 🟢 Green badge | Standard operating range. Baseline attention density. |
| `50,000 – 80,000` | `75 – 120` | **Caution** | 🟡 Amber badge | Attention dilution risk elevated; avoid large log dumps. |
| `> 80,000` | `> 120` | **Critical** | 🔴 Red badge + alert | High attention dilution risk. Session fork recommended. |

---

## ⚡ 4. Feature 2: Native "Summarize & Fork" (The 6-Part Schema)

ContextFork transforms session restarts from a destructive wipe into an intelligent, verifiable checkpoint.

### 4.1. The Forking Workflow

```mermaid
flowchart LR
    A["Bloated Session (120k tokens)"] --> B["Click: '⚡ Summarize & Fork'"]
    B --> C["Agent Generates 6-Part Handoff"]
    B --> D["Git Diff & State Auto-Exported"]
    C & D --> E["New Clean Session Auto-Launched"]
    E --> F["Work Resumes with Architectural Continuity"]
```

### 4.2. The 6-Part Structured Handoff Schema
When triggered, a structured synthesis is generated complying with [`session-handoff.schema.json`](schemas/session-handoff.schema.json):

```markdown
# 🔄 Session Handoff Checkpoint (Forked from Session <ID>)

### 1. Active Goal & Scope
* Exact feature, bug, or architectural milestone being developed.
* Concrete acceptance criteria.

### 2. Settled Decisions, Tradeoffs & Evidence (Provenance)
* Accepted architectural choices and their rationale.
* Explicitly rejected alternatives (prevents re-debating).
* **Evidence:** File paths, commit hashes, or test results proving this decision
  (e.g. `src/Auth/JwtService.cs`, Commit `7ed9b80`).

### 3. Working Tree State & Machine Git Metadata
* **Git Status:** HEAD commit, current branch, clean/dirty state.
* **Modified Files:** Exact files created, modified, or deleted (`git diff HEAD --stat`).

### 4. Failed Approaches & Known Pitfalls (⚡ Anti-Loop Shield)
* What was attempted, what failed, and why it was abandoned.
* **Barred Action:** Explicit instruction prohibiting the child agent from retrying
  this failed approach.

### 5. Open Risks, Edge Cases & Unknowns
* Lingering technical risks, external dependencies, or unverified assumptions.

### 6. Immediate Next Action
* The single, atomic next command, test, or code edit to execute immediately.
```

> **Why section 4 ("Failed Approaches") is the most critical:**  
> Without it, a child agent has no record of dead ends. It will re-examine the same failed paths, wasting cycles and regressing. This section is the protocol's primary defense against repetitive failure loops.

---

## 🛡️ 5. Verifiable Handoff Package (Deterministic State)

A text-only LLM summary is vulnerable to omission or drift. Because `git diff HEAD` does NOT capture newly created, untracked files, ContextFork specifies a **Verifiable Handoff Package** complying with [`verifiable-package.schema.json`](schemas/verifiable-package.schema.json) persisted automatically on fork:

```text
.contextfork/
├── handoff_summary.md       # Synthesized 6-part markdown handoff (LLM intent)
├── git_status.json          # Untracked, staged, and modified files (Machine truth)
├── git_diff.patch           # Exact working-tree diff against parent HEAD (Staged + Unstaged)
├── untracked_manifest.json  # Manifest of untracked files (path, size, sha256)
├── untracked/               # Snapshots of new/untracked text files (< 1MB)
└── session_metadata.json    # Parent ID, token counts, step duration, full commit SHA
```

When the child session initializes, it reads the synthesized markdown for intent, while anchoring its physical perception in the deterministic diff, status, and untracked file snapshots.

> **Design Rationale:** The hybrid model — LLM summary paired with machine-generated git artifacts — ensures that even if the summary is imprecise, the child agent can independently verify the actual state of the working tree.

---

## 💻 6. Reference Implementation (`contextfork.py`)

ContextFork provides an official reference CLI written in pure Python 3.10+ standard library, demonstrating that the IPSF method is buildable with zero external dependencies.

> **Scope Note:** `contextfork.py` is a **reference implementation** — its purpose is to demonstrate the protocol's feasibility and serve as a specification artifact, not to be a production-hardened tool. Independent implementations by IDE vendors and toolchain authors are explicitly encouraged.

```bash
# Show repository state and active .contextfork package info
python contextfork.py status

# Export a verifiable handoff package (diff, status, untracked files)
python contextfork.py export --session "session-123" --goal "Refactoring Auth Service"

# Validate package structure and integrity
python contextfork.py validate

# Run an interactive terminal demonstration
python contextfork.py demo
```

### How to Fill the Handoff Template
The `handoff_summary.md` is a structured template. In an AI-assisted workflow:
1. Run `python contextfork.py export` to capture the machine state.
2. Ask the LLM: *"Read `.contextfork/handoff_summary.md` and fill in sections 1–4 and 6 based on our session history."*
3. The LLM fills the intent-level sections (Goal, Decisions, Failed Approaches, Next Action).
4. Section 3 (Working Tree State) is pre-populated from `git_status.json` by the export command.
5. Commit or copy `.contextfork/` into the new session's context.

---

## 📐 7. Formal Protocol Schemas (`schemas/`)

ContextFork provides formal JSON Schemas for tool authors and IDE vendors to implement interoperable context lifecycle management:

| Schema File | Purpose |
| :--- | :--- |
| **[`session-handoff.schema.json`](schemas/session-handoff.schema.json)** | Validates the 6-part handoff summary with evidence and provenance fields. |
| **[`context-policy.schema.json`](schemas/context-policy.schema.json)** | Defines configurable warning/fork token thresholds and step heuristics. |
| **[`context-telemetry.schema.json`](schemas/context-telemetry.schema.json)** | Validates the ambient context telemetry payload emitted to IDE UI. |
| **[`verifiable-package.schema.json`](schemas/verifiable-package.schema.json)** | Validates the structure and file manifests of the `.contextfork/` bundle. |

---

## 🧭 8. The Architecture: Agent Context System (ACS)

ContextFork operates within a unified three-tier **Agent Context & Efficiency Stack**:

```text
               ┌────────────────────────────────────────────────────────┐
               │         AGENT CONTEXT & EFFICIENCY STACK               │
               └────────────────────────────────────────────────────────┘
                                           │
         ┌─────────────────────────────────┼─────────────────────────────────┐
         ▼                                 ▼                                 ▼
   [ RETRIEVE ]                      [ MANAGE ]                        [ TRANSFER ]
  git-grep-first                    ContextFold                        ContextFork
  (Search Policy)            (Virtual Memory Paging)             (Session Handoff & Shaping)
  • Zero token bloat         • In-place folding                  • 6-part verifiable handoff
  • git grep --untracked     • UI side-drawers                   • Evidence & Git state
  • Anti-Select-String       • On-demand hydration               • Multi-agent context shaping
```

Beyond temporal continuity (Session A → Session B), ContextFork enables **role-based context shaping**: filtering the parent session's context into specialized slices for subagents (Planner, Coder, Tester), so each agent receives only the context relevant to its role.

---

## 🔌 9. Integration Surfaces

The specification is designed for modular adoption across multiple developer interface layers:

* **IDE Platforms (Antigravity, Cursor, Windsurf):**
  * Ambient status-bar badge for live context telemetry.
  * Toolbar action replacing destructive "New Chat" with "Fork Chat with Checkpoint".
  * Drawer panel showing verifiable `.contextfork/` artifacts.
* **CLI & Terminal Tools (Claude Code, Antigravity CLI, Aider):**
  * Native `/fork` or `--fork` commands to seed a clean child session from the current state.
* **Agent Orchestration Frameworks (LangChain, AutoGen, CrewAI):**
  * Context lifecycle middleware and session state provider for subagent context shaping.

> 🎨 **Concept mockup — not a screenshot.** This image illustrates the target IDE integration described in [§9 Integration Surfaces](#-9-integration-surfaces). No graphical implementation exists yet — `contextfork.py` is a terminal-only reference CLI (see [§6 Reference Implementation](#-6-reference-implementation-contextforkpy)). The UI shown here is a design goal, not a working feature.

![Concept mockup of the target ContextFork IDE integration — illustrative, not a working screenshot](docs/concept-mockup-target-ui.png)

---

## 🗺️ 10. Relationship to Prior & Related Work

ContextFork is not the first work in session continuity and context management. This section clarifies what the protocol adds:

| Prior Work | What it does | What IPSF adds |
| :--- | :--- | :--- |
| **Claude Code `/compact`** | Summarizes the session in-place, replacing old messages | IPSF generates an *external*, machine-verifiable package; the parent session is preserved and tagged, not destructively modified |
| **Amp Handoff** | Structured handoff between sessions | IPSF adds the `failed_approaches` Anti-Loop Shield, provenance-linked evidence, and untracked file capture via the Verifiable Package |
| **MemGPT / Letta** | Agent memory with hierarchical paging (main context + archival memory) | IPSF focuses on *session transfer* rather than runtime memory management; ContextFold (a sibling spec) covers in-session virtual memory paging |
| **LangGraph Checkpoints** | Deterministic graph state snapshots for resumability | IPSF is LLM-native and IDE-level; it captures *intent* (handoff markdown) alongside *state* (git artifacts), targeting developer workflow rather than agent graph internals |
| **`git stash` / `git bundle`** | Git-native state preservation | IPSF orchestrates git artifacts at the *session protocol* level, pairing them with structured LLM-generated summaries the child agent can read directly |

**The IPSF contribution:** The combination of (1) structured 6-part schema with explicit `failed_approaches`, (2) provenance-linked evidence, (3) untracked file capture filling the `git diff HEAD` gap, and (4) a vendor-neutral open schema enabling cross-platform implementation.

---

## 📜 11. License & Attribution

Released under the **[MIT License](LICENSE)**.

```bibtex
@misc{kilinc2026contextfork,
  author = {Mustafa KILINC (@mrblackman)},
  title  = {ContextFork: In-Place Session Forking Protocol (IPSF-1.2)},
  year   = {2026},
  publisher = {GitHub},
  howpublished = {\url{https://github.com/mrblackman/ContextFork}}
}
```
