---
name: scope
description: "Codebase awareness tool: ranked symbols by cross-file importance for project orientation and post-edit verification. Use when: entering an unfamiliar repo, looking for entry points and high-impact symbols, finding source↔test pairs, or verifying structure after edits. Trigger words: map, codebase, structure, overview, orientation, understand the project."
compatibility: "Requires `scope` CLI on PATH. Install: `cd ~/scope && uv tool install .`"
---

# Repo Baby — Codebase Map for Agent Orientation

## Prerequisites

```bash
# Check if installed
which scope

# Install (one-time)
cd ~/scope && uv tool install .

# Run from anywhere
scope --path /some/repo --mode overview
```

## What This Tool Does

Repo Baby scans a codebase with Tree-sitter (25+ languages), extracts all symbols (functions, classes, methods, interfaces, etc.), ranks them by cross-file reference importance, and returns the result in a compact text format.

It has three modes, each covering something an agent cannot efficiently do with bash alone.

## WHEN TO CALL (exactly four triggers)

**1. ORIENTATION** — you just entered a repo for the first time this session, or the user asked an open-ended question and you cannot name the top 3 relevant files. One call replaces 5-10 turns of ls/grep/read.
→ `scope --path <repo> --mode overview` (frameworks, entrypoints, stats, suggested reads)
→ Then `scope --path <repo> --mode map --token-budget 800` (structural detail)

**2. VERIFICATION** — you just renamed a function, moved a class, changed an export, or modified a shared interface across multiple files.
→ `scope --path <repo> --mode map` shows fresh importance scores — does the refactor look correct?

**3. CONTEXT SWITCH** — the user said "look at the auth module instead" or switched branches/topics. Your mental map is stale.
→ `scope --path <repo> --mode overview` or `--mode map` to re-orient.

**4. STRUCTURAL QUESTION** — the user asked "how is this organized?", "what depends on what?", "where are the tests for X?"
→ `scope --path <repo> --mode pairs` for test/source mapping
→ `scope --path <repo> --mode map` for ranked symbols

## DO NOT CALL when

- The user named a specific file and line number
- The task is a one-line change in a file you have already read
- You called it within the last 3 turns and the repo has not changed
- The task is purely mechanical: formatting, versions, comments, simple typos
- You are in "execute" mode, not "orient" mode — you already know where to edit

## AFTER CALLING

- Always read "suggested next reads" — they are ranked by importance
- If mode=map shows high-importance symbols, read those files next
- If mode=pairs returned test files for a source you are editing, run those tests

## Mode Reference

| Mode | Command | What it shows | Why not bash |
|---|---|---|---|
| overview | `--mode overview` | Frameworks, entrypoints, language stats, suggested reads, package scripts | Replaces 3-4 chained exploration commands |
| map | `--mode map --token-budget 800` | Ranked symbols by cross-file reference count, grouped by file | Bash cannot compute cross-file importance |
| pairs | `--mode pairs` | Source↔test file matching | Tedious to do with grep/find name matching |

## Common Flags

| Flag | Purpose |
|---|---|
| `--path <dir>` | Repository root (required) |
| `--scope <subdir>` | Limit to a subdirectory (e.g. `src/`) |
| `--token-budget <n>` | Output size limit in tokens (default 800) |
| `--max-files <n>` | Maximum source files to scan (default 1000) |
| `--format json` | Structured JSON output |
| `--no-cache` | Bypass symbol cache |

## Output Example

```text
- src/auth/service.py:
  function validate_token (line 45)  ← 12 files
  function refresh_session (line 102)  ← 5 files
  class TokenManager (line 15)
- src/api/handlers.py:
  function login_handler (line 23)  ← 3 files
## Suggested next reads
1. src/auth/service.py
2. src/api/handlers.py
```

The `← N files` shows cross-file reference count. Higher means more files depend on this symbol — read it first.
