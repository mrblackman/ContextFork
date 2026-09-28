---
name: contextfork
description: In-place session forking & checkpoint protocol (IPSF-1.2) for Antigravity AI agents to prevent context bloat and manage conversation lineage.
---

# 🍴 ContextFork (IPSF-1.2) — Antigravity Agent Skill

This skill enables AI agents in Antigravity to execute deterministic session handoffs, conversation lineage naming (`-CF-YYMMDD-HHMM`), and timestamped snapshot archive management with zero UI friction.

---

## 🛑 STRICT RULES

1. **NO FAKE HTML BUTTONS (ZF-022 SHIELD):**
   * NEVER output raw HTML `<button onclick="...">` in chat. Antigravity's Markdown sanitizer (DOMPurify) strips all JavaScript attributes, rendering the buttons completely dead and broken.
2. **NATIVE MODALS VIA `ask_question`:**
   * Whenever user interaction, selection, or confirmation is required, ALWAYS invoke the native `ask_question` tool. This renders Antigravity's native dialog with a functional **[ Submit ]** button.
3. **ZERO-REDUNDANCY CONFIRMATION:**
   * When the user selects an export or resume action from an `ask_question` menu, that selection is their FINAL consent. NEVER ask a redundant second confirmation question.
4. **NO-SILENT-RESUME RULE:**
   * When resuming context (`/contextfork resume`), NEVER simply print the single-line `> [!NOTE]` callout and stop. You MUST visibly display the recovered goals, settled decisions, and Git status to the user.
5. **SESSION_ID INGESTION & LINEAGE PROVENANCE:**
   * Always pass the active conversation's `session_id` parameter to BOTH `export_session_context(session_id=...)` AND `resume_session_context(session_id=...)`.
   * On export: leave `parent_title=""` so `contextfork_mcp.py` automatically resolves the real conversation title with 100% precision. NEVER invent arbitrary titles from chat greetings.
   * On resume: always pass `session_id="<current-session-id>"` so `consumed_by` metadata records true lineage provenance.
6. **AUTONOMOUS PROMOTE BAN:**
   * AI agents are STRICTLY FORBIDDEN from running `python scripts/deploy_skill.py --promote`. Promotion to the live developer profile requires an interactive terminal (TTY) with manual commit hash confirmation.

---

## 🎯 Command Set & Workflows

### 1. Bare Command (`/contextfork`)
When the user simply types `/contextfork`:
1. Call `get_context_status()`.
2. Inspect the output. If there are pending (`unconsumed`) snapshots waiting in the archive:
   - Call `ask_question`:
     * **question:** `"A pending ContextFork handoff package was detected. How would you like to proceed?"`
     * **options:**
       1. `"(Recommended) Resume previous session memory (/contextfork resume)"`
       2. `"Export a new checkpoint package (/contextfork export)"`
       3. `"List archived package details (/contextfork details)"`
   - **Branching Logic:**
     * **If Option 1 selected:** Proceed immediately to **Resume Workflow** (Step 3).
     * **If Option 2 selected:** This IS the user's explicit consent! **DO NOT ASK A SECOND CONFIRMATION QUESTION.** Immediately call `export_session_context(goal="[Brief Goal]")`.
     * **If Option 3 selected:** Immediately call `get_package_details()`.
3. If NO pending snapshot exists in the archive:
   - Call `ask_question`:
     * **question:** `"Do you confirm creating a new Checkpoint package?"`
     * **options:**
       1. `"(Recommended) Yes, seal and proceed"`
       2. `"No, cancel operation"`
   - If Option 1 selected: Call `export_session_context(goal="[Brief Goal]")`.
   - If Option 2 selected: Terminate cleanly without writing any files.

---

### 2. Fork & Snapshot Session (`/contextfork export`)
When the user explicitly invokes `/contextfork export` directly (without using the `/contextfork` menu):
1. Prompt for confirmation using native `ask_question`:
   * **question:** `"Do you confirm creating a new Checkpoint package?"`
   * **options:**
     1. `"(Recommended) Yes, seal and proceed"`
     2. `"No, cancel operation"`
2. **Branching Logic:**
   * **If User selects `"No, cancel operation"` (or cancels):**
     - DO NOT call any writing tools.
     - Print: `🚫 Checkpoint operation cancelled by user.`
     - Terminate the task cleanly (0 bytes mutated).
   * **If User selects `"(Recommended) Yes, seal and proceed"`:**
     - Call `export_session_context(goal="[Brief summary of current session goal and key decisions]")`.
     - **CRITICAL FORMATTING MANDATE:** Directly output the Handoff Card returned by `export_session_context`. You are STRICTLY FORBIDDEN from omitting the `Snapshot File` clickable link. The user must see the clickable link `[YYMMDD-HHMMSS (handoff_summary.md)](file:///...)`.

---

### 3. Resume in Continuation Session (`/contextfork resume`)
When the user opens a fresh session and runs `/contextfork resume`:
1. Call `get_context_status()`.
2. Check pending snapshot telemetry:
   - **If MULTIPLE pending (`unconsumed`) snapshots exist (> 1) [ZF-025]:**
     Call `get_package_details()` to inspect pending packages, then call `ask_question`:
     * **question:** `"Multiple pending handoff packages detected. Which one would you like to load?"`
     * **options:** List at most the **top 3 latest pending snapshots** (e.g. `"(Recommended) Latest package: <ID> - <Title>"`, `"<ID2> - <Title2>"`, `"<ID3> - <Title3>"`). If more than 3 pending snapshots exist, append: `"+ N more packages exist — Run /contextfork details for full list"`, `"Clean old packages (/contextfork clean)"`, `"Cancel"`.
     Pass the selected `package_id` and active `session_id` to `resume_session_context(package_id=..., session_id="<current-session-id>")`.
   - **If EXACTLY 1 pending snapshot exists (or none):**
     DO NOT ask any question! Immediately call `resume_session_context(session_id="<current-session-id>")`. If 0 pending packages exist, `resume_session_context()` will safely display the archive status table without modifying metadata.
3. **MANDATORY VISIBLE RESUME DISPLAY (NO-SILENT-RESUME RULE):**
   * YOU ARE STRICTLY FORBIDDEN from merely outputting the single-line `> [!NOTE]` callout!
   * You MUST visibly print the recovered context in a clean, structured table/card before the closing note:
     - 🎯 **Session Goal & Scope (Recovered Goal)**
     - 🏛️ **Settled Architectural Decisions**
     - 🌿 **Git Status & HEAD Verification**
   * After displaying this structured recovery summary, append the standard closure:
     ```markdown
     > [!NOTE]
     > Context successfully synchronized. You may now continue your work with a fresh context.
     ```

---

### 4. Inspect Snapshot Details (`/contextfork details` or `PackDetails`)
When the user wants to inspect all archived snapshots:
* Call `get_package_details()` and render the structured checkpoint table.

---

### 5. Archive Cleanup (`/contextfork clean`)
When the user wants to remove consumed snapshot packages:
1. Call `clean_session_context(confirm=False)` for a dry-run check.
2. If consumed packages exist, call `ask_question`:
     * **question:** `"Do you want to permanently delete consumed snapshot packages?"`
     * **options:**
       1. `"(Recommended) Yes, clean old packages"`
       2. `"No, keep in archive"`
3. If confirmed, call `clean_session_context(confirm=True)`.
