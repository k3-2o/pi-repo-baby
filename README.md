# Scope

Codebase awareness CLI: ranked symbols by cross-file importance, project overview, and test/source pair mapping.

```bash
scope --path /some/repo --mode overview
scope --path /some/repo --mode map --token-budget 800
scope --path /some/repo --mode pairs
```

Designed for AI coding agents that need to orient themselves in an unfamiliar codebase. Three modes, each covering something bash cannot efficiently do alone.

## Install

```bash
# Requires Python 3.11+ and uv
cd scope  # or wherever you cloned it
uv tool install .
```

Then `scope` is on your PATH permanently.

## Modes

| Mode | What it does | Why not bash |
|---|---|---|
| `overview` | Frameworks, entrypoints, language stats, suggested reads, package scripts | Replaces 3-4 chained exploration commands |
| `map` | Ranked symbols (functions/classes/methods) by cross-file reference count, grouped by file | Bash cannot compute cross-file importance |
| `pairs` | Source↔test file mapping | Tedious to do with grep/find name matching |

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--path DIR` | required | Repository root |
| `--scope DIR` | `.` | Limit to a subdirectory |
| `--token-budget N` | 800 | Output size limit in tokens |
| `--max-files N` | 1000 | Maximum source files to scan |
| `--mode MODE` | `map` | `map`, `overview`, or `pairs` |
| `--format FMT` | `text` | `text` or `json` |
| `--no-cache` | false | Bypass symbol cache |

## Output

```
- src/auth/service.py:
  function validate_token (line 45)  ← 12 files
  function refresh_session (line 102)  ← 5 files
  class TokenManager (line 15)
## Suggested next reads
1. src/auth/service.py
2. src/api/handlers.py
```

The `← N files` shows how many other files reference each symbol.

## How it works

1. Discovers source files (git-tracked or filesystem walk)
2. Parses with Tree-sitter (25+ languages: Python, JS, TS, Go, Rust, Java, Ruby, C/C++, PHP, Kotlin, Swift, Scala, Bash, SQL, Lua, HCL, and more)
3. Extracts symbols with scope tracking (class.method prefixing)
4. Ranks by cross-file token reference count
5. Caches to `.git/scope-cache-v2.json` (invalidated on file changes)

## Pi Agent Skill

A skill at `~/.pi/agent/skills/scope/SKILL.md` teaches the agent when to call the tool and how to interpret output. See `docs/SKILL.md` for the source.

## License

MIT
