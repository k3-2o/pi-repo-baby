#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import tomllib
except ImportError:  # pragma: no cover - py<3.11 compatibility
    tomllib = None  # type: ignore[assignment]

_TS_PACK_AVAILABLE = False
try:
    from tree_sitter import Language, Parser, Node
    from tree_sitter_language_pack import get_language
    _TS_PACK_AVAILABLE = True
except ImportError:
    pass

_EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".sc": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".lua": "lua",
    ".tf": "hcl",
    ".tfvars": "hcl",
    ".hcl": "hcl",
}

_PARSERS: Dict[str, Optional[Parser]] = {}

IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", "target", ".terraform", ".idea", ".vscode",
    "vendor", "bin", "obj", "out", ".next", ".nuxt", ".cache",
    "coverage", "htmlcov", ".tox", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".hypothesis", ".hg", ".svn", "site-packages",
})

SUPPORTED_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx",
    ".go", ".rs", ".rb", ".java",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hxx",
    ".cs", ".php", ".kt", ".kts", ".swift",
    ".scala", ".sc", ".sh", ".bash", ".sql", ".lua",
    ".tf", ".tfvars", ".hcl",
})

SKIP_FILE_PATTERNS = [
    re.compile(r"package-lock\.json$", re.IGNORECASE),
    re.compile(r"yarn\.lock$", re.IGNORECASE),
    re.compile(r"pnpm-lock\.yaml$", re.IGNORECASE),
    re.compile(r"\.min\.(js|css)$", re.IGNORECASE),
    re.compile(r"go\.sum$"),
    re.compile(r"Gemfile\.lock$"),
    re.compile(r"poetry\.lock$"),
    re.compile(r"uv\.lock$"),
    re.compile(r"\.d\.ts$"),
]

MAX_FILE_SIZE = 500_000
MAX_FILES_DEFAULT = 1000

ENTRYPOINT_NAMES = frozenset({
    "main", "index", "app", "server", "cli", "cmd", "handler", "manage",
    "wsgi", "asgi", "router", "routes",
})

CONFIG_FILENAMES = frozenset({
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "settings.gradle",
    "composer.json", "Gemfile", "Makefile", "Dockerfile", "docker-compose.yml",
    "terraform.tf", "main.tf", "variables.tf", "outputs.tf",
})

TEST_MARKERS = ("/test/", "/tests/", "/__tests__/", ".test.", ".spec.")


def git_tracked_files(repo_path: str) -> List[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def normalize_scope(scope: str) -> str:
    scope = (scope or ".").strip().replace("\\", "/")
    if scope in ("", "."):
        return "."
    scope = scope.lstrip("/")
    normalized = os.path.normpath(scope).replace("\\", "/")
    if normalized.startswith("..") or normalized == ".":
        return "."
    return normalized.rstrip("/")


def walk_files(repo_path: str, scope: str = ".") -> List[str]:
    files: List[str] = []
    scope = normalize_scope(scope)
    scope_path = Path(repo_path) / scope

    if not scope_path.exists():
        return files

    for root, dirs, filenames in os.walk(scope_path):
        dirs[:] = sorted(
            d for d in dirs
            if d not in IGNORE_DIRS and not d.startswith(".")
        )

        for filename in filenames:
            if filename.startswith("."):
                continue

            full_path = Path(root) / filename
            rel_path = str(full_path.relative_to(repo_path))

            if not _should_include_file(rel_path):
                continue

            files.append(rel_path)

    return files


def discover_files(repo_path: str, scope: str = ".") -> List[str]:
    scope = normalize_scope(scope)
    git_files = git_tracked_files(repo_path)
    if git_files:
        result = []
        for f in git_files:
            if scope != "." and not f.startswith(scope.rstrip("/") + "/") and f != scope.rstrip("/"):
                continue
            if _should_include_file(f):
                result.append(f)
        return result

    return walk_files(repo_path, scope)


def get_parser(ext: str) -> Optional[Parser]:
    if not _TS_PACK_AVAILABLE:
        return None

    if ext in _PARSERS:
        return _PARSERS[ext]

    parser: Optional[Parser] = None
    lang_name = _EXT_TO_LANG.get(ext)

    if lang_name:
        try:
            lang = get_language(lang_name)
            parser = Parser(lang)
        except Exception as e:
            print(f"[repo-baby] Could not load parser for {ext} ({lang_name}): {e}", file=sys.stderr)
            parser = None

    _PARSERS[ext] = parser
    return parser


def _walk_tree(node: "Node", ext: str) -> List[Tuple[str, str, int]]:
    symbols: List[Tuple[str, str, int]] = []
    parent_class: List[str] = []

    def visit(n: "Node", depth: int = 0):
        nonlocal parent_class
        if depth > 200 or n is None:
            return

        name_node = n.child_by_field_name("name")

        if name_node is not None:
            name = name_node.text.decode("utf-8", errors="replace")
            line = name_node.start_point[0] + 1

            ntype = n.type
            if ntype in ("function_definition", "async_function_definition"):
                kind = "method" if parent_class else "function"
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append((kind, full_name, line))
            elif ntype == "class_definition":
                symbols.append(("class", name, line))
                parent_class.append(name)
            elif ntype == "function_declaration":
                kind = "method" if parent_class else "function"
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append((kind, full_name, line))
            elif ntype == "class_declaration":
                symbols.append(("class", name, line))
                parent_class.append(name)
            elif ntype == "interface_declaration":
                symbols.append(("interface", name, line))
                parent_class.append(name)
            elif ntype == "method_definition":
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append(("method", full_name, line))
            elif ntype == "method_declaration":
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append(("method", full_name, line))
            elif ntype == "public_field_definition":
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append(("method", full_name, line))
            elif ntype == "variable_declarator" and ext in (".js", ".ts", ".tsx"):
                value_node = n.child_by_field_name("value")
                if value_node is not None:
                    value_type = value_node.type
                    full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                    if value_type in ("arrow_function", "function", "function_expression"):
                        kind = "method" if parent_class else "function"
                        symbols.append((kind, full_name, line))
                    elif value_type in ("class", "class_expression"):
                        symbols.append(("class", name, line))
            elif ntype == "type_alias_declaration":
                symbols.append(("type", name, line))
            elif ntype == "enum_declaration":
                symbols.append(("enum", name, line))
            elif ntype == "type_spec":
                has_interface = any(c.type == "interface_type" for c in n.children)
                kind = "interface" if has_interface else "struct"
                symbols.append((kind, name, line))
            elif ntype == "function_item":
                kind = "method" if parent_class else "function"
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append((kind, full_name, line))
            elif ntype == "struct_item":
                symbols.append(("struct", name, line))
            elif ntype == "trait_item":
                symbols.append(("trait", name, line))
                parent_class.append(name)
            elif ntype == "enum_item":
                symbols.append(("enum", name, line))
            elif ntype == "struct_specifier" and ext in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hxx"):
                symbols.append(("struct", name, line))
            elif ntype == "class_specifier" and ext in (".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".cs"):
                symbols.append(("class", name, line))
                parent_class.append(name)
            elif ntype == "class" and ext in (".rb"):
                symbols.append(("class", name, line))
                parent_class.append(name)
            elif ntype == "module" and ext in (".rb"):
                symbols.append(("module", name, line))
                parent_class.append(name)
            elif ntype == "method" and ext in (".rb"):
                full_name = f"{parent_class[-1]}.{name}" if parent_class else name
                symbols.append(("method", full_name, line))

        if n.type == "block" and ext in (".tf", ".tfvars", ".hcl"):
            children = n.children
            if len(children) >= 1:
                block_type = children[0].text.decode("utf-8", errors="replace")
                for child in children[1:]:
                    if child.type in ("string_lit", "template_string"):
                        raw = child.text.decode("utf-8", errors="replace")
                        name = raw.strip('"').strip("'")
                        line = child.start_point[0] + 1
                        kind = block_type
                        symbols.append((kind, name, line))
                        break

        if n.type == "impl_item" and ext == ".rs":
            for child in n.children:
                if child.type == "type_identifier":
                    impl_name = child.text.decode("utf-8", errors="replace")
                    line = child.start_point[0] + 1
                    symbols.append(("impl", impl_name, line))
                    parent_class.append(impl_name)
                    break

        _has_name = n.child_by_field_name("name") is not None
        entering_scope = (
            n.type in (
                "class_definition", "class_declaration", "interface_declaration",
                "impl_item", "trait_item", "class_specifier",
            )
            or (n.type == "class" and ext in (".rb",) and _has_name)
            or (n.type == "module" and ext in (".rb",) and _has_name)
        )

        for child in n.children:
            visit(child, depth + 1)

        if entering_scope and parent_class:
            parent_class.pop()

    visit(node)
    return symbols


class Symbol:
    __slots__ = ("name", "kind", "file", "line", "importance", "refs")

    def __init__(self, name: str, kind: str, file: str, line: int):
        self.name = name
        self.kind = kind
        self.file = file
        self.line = line
        self.importance: float = 0.0
        self.refs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "importance": self.importance,
            "refs": self.refs,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Symbol":
        sym = cls(str(data["name"]), str(data["kind"]), str(data["file"]), int(data["line"]))
        sym.importance = float(data.get("importance", 0.0))
        sym.refs = int(data.get("refs", 0))
        return sym


def _should_include_file(rel_path: str) -> bool:
    base = Path(rel_path).name
    ext = Path(rel_path).suffix

    for pattern in SKIP_FILE_PATTERNS:
        if pattern.search(rel_path):
            return False

    if base in CONFIG_FILENAMES or base.lower().startswith("readme."):
        return True

    if ext not in SUPPORTED_EXTENSIONS:
        return False

    return True


def _is_test_file(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/")
    base = os.path.basename(p)
    return (
        any(marker in f"/{p}" for marker in TEST_MARKERS)
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base.endswith("_test.go")
    )


def _is_config_or_entrypoint(rel_path: str) -> bool:
    path = Path(rel_path)
    base = path.name
    stem = path.stem.lower()
    return base in CONFIG_FILENAMES or stem in ENTRYPOINT_NAMES


def _is_doc_file(rel_path: str) -> bool:
    base = Path(rel_path).name.lower()
    return base.startswith("readme.") or Path(rel_path).suffix.lower() in (".md", ".mdx", ".rst")


def _file_priority(rel_path: str) -> Tuple[int, int, int, str]:
    """Lower sorts earlier. Keeps small-project entry points visible and avoids
    large-repo caps being consumed by generated/test files first."""
    path = rel_path.replace("\\", "/")
    parts = path.split("/")
    score = 50
    if _is_config_or_entrypoint(path):
        score -= 25
    if parts[0] in ("src", "lib", "app", "packages", "cmd", "internal"):
        score -= 10
    if _is_test_file(path):
        score += 25
    if any(part in IGNORE_DIRS for part in parts):
        score += 50
    depth = path.count("/")
    return (score, depth, len(path), path)


def prioritize_files(files: Iterable[str]) -> List[str]:
    return sorted(files, key=_file_priority)


def language_stats(files: Iterable[str]) -> Dict[str, int]:
    stats: Dict[str, int] = defaultdict(int)
    for file_path in files:
        lang = _EXT_TO_LANG.get(Path(file_path).suffix, "other")
        stats[lang] += 1
    return dict(sorted(stats.items(), key=lambda item: (-item[1], item[0])))


_IDENTIFIER_RE = re.compile(r"[a-zA-Z_]\w+")


def _is_dunder_base(name: str) -> bool:
    base = name.rsplit(".", 1)[-1]
    return base.startswith("__") and base.endswith("__")


def _tokenize_identifiers(content: str):
    for match in _IDENTIFIER_RE.finditer(content):
        yield match.group(0)


def extract_symbols(file_path: str, repo_path: str) -> List[Symbol]:
    ext = Path(file_path).suffix
    full_path = os.path.join(repo_path, file_path)

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except (OSError, IOError):
        return []

    if len(content) > MAX_FILE_SIZE:
        return []
    if len(content) > 10_000 and content.count("\n") < 10:
        return []

    parser = get_parser(ext)
    if parser is None:
        return []

    raw: List[Tuple[str, str, int]] = []
    try:
        tree = parser.parse(bytes(content, "utf-8"))
        raw = _walk_tree(tree.root_node, ext)
    except Exception as e:
        print(f"[repo-baby] tree-sitter error on {file_path}: {e}", file=sys.stderr)
        return []

    rel_path = file_path
    return [
        Symbol(name, kind, rel_path, line)
        for kind, name, line in raw
        if not _is_dunder_base(name)
    ]


def _reference_tokens(name: str) -> Set[str]:
    base = name.rsplit(".", 1)[-1]
    tokens = {base}
    if "." not in name:
        tokens.add(name)
    return {token for token in tokens if _IDENTIFIER_RE.fullmatch(token)}


def build_symbol_index(all_symbols: Dict[str, List[Symbol]]) -> Dict[str, List[Symbol]]:
    index: Dict[str, List[Symbol]] = defaultdict(list)
    for symbols in all_symbols.values():
        for sym in symbols:
            for token in _reference_tokens(sym.name):
                index[token].append(sym)
    return dict(index)


def compute_importance(
    all_symbols: Dict[str, List[Symbol]],
    repo_path: str,
    file_inrefs: Optional[Dict[str, int]] = None,
) -> None:
    index = build_symbol_index(all_symbols)

    all_tokens = list(index.keys())
    if not all_tokens:
        return
    name_set = frozenset(all_tokens)

    ref_count: Dict[Tuple[str, str], int] = defaultdict(int)

    for file_path, symbols in all_symbols.items():
        full_path = os.path.join(repo_path, file_path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except (OSError, IOError):
            continue

        words_in_file: Set[str] = set()
        for word in _tokenize_identifiers(content):
            if word in name_set and word not in words_in_file:
                words_in_file.add(word)
                for sym in index[word]:
                    if sym.file != file_path:
                        ref_count[(sym.file, sym.name)] += 1

    for file_path, symbols in all_symbols.items():
        is_test = _is_test_file(file_path)

        for sym in symbols:
            score = float(ref_count.get((sym.file, sym.name), 0))
            sym.refs = int(score)

            if sym.kind in ("class", "interface"):
                score *= 1.5
            elif sym.kind in ("resource", "module", "data"):
                score *= 2.0
            elif sym.kind == "key":
                score = -1.0

            base_name = sym.name.rsplit(".", 1)[-1]
            if base_name in ("main", "index", "App", "Server",
                             "setup", "configure", "create_app", "handler"):
                score += 5.0

            if file_inrefs:
                score += min(file_inrefs.get(file_path, 0), 25) * 0.35

            if is_test or base_name.startswith("test_") or base_name.startswith("it("):
                score *= 0.05

            sym.importance = score


def symbols_to_dict(all_symbols: Dict[str, List[Symbol]]) -> Dict[str, List[Dict[str, Any]]]:
    return {file_path: [sym.to_dict() for sym in symbols] for file_path, symbols in all_symbols.items()}


def symbols_from_dict(data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Symbol]]:
    return {file_path: [Symbol.from_dict(sym) for sym in symbols] for file_path, symbols in data.items()}


def cache_dir(repo_path: str) -> Path:
    git_dir = Path(repo_path) / ".git"
    if git_dir.is_dir():
        return git_dir
    fallback = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "repo-baby"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def cache_file(repo_path: str) -> Path:
    return cache_dir(repo_path) / "repo-baby-cache-v2.json"


def snapshot_file(repo_path: str) -> Path:
    return cache_dir(repo_path) / "repo-baby-last-symbols-v2.json"


def git_head(repo_path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True,
            text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return "nogit"


def files_signature(repo_path: str, files: List[str]) -> str:
    h = hashlib.sha256()
    h.update(git_head(repo_path).encode())
    for rel_path in files:
        full_path = Path(repo_path) / rel_path
        try:
            stat = full_path.stat()
        except OSError:
            continue
        h.update(rel_path.encode())
        h.update(str(stat.st_mtime_ns).encode())
        h.update(str(stat.st_size).encode())
    return h.hexdigest()


def load_cached_symbols(repo_path: str, files: List[str], scope: str, max_files: int) -> Optional[Dict[str, List[Symbol]]]:
    path = cache_file(repo_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("signature") != files_signature(repo_path, files):
        return None
    if payload.get("scope") != normalize_scope(scope) or payload.get("max_files") != max_files:
        return None
    return symbols_from_dict(payload.get("symbols", {}))


def save_cached_symbols(repo_path: str, files: List[str], scope: str, max_files: int, all_symbols: Dict[str, List[Symbol]]) -> None:
    payload = {
        "version": 2,
        "signature": files_signature(repo_path, files),
        "scope": normalize_scope(scope),
        "max_files": max_files,
        "symbols": symbols_to_dict(all_symbols),
    }
    try:
        cache_file(repo_path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def load_snapshot(repo_path: str) -> Dict[str, List[Symbol]]:
    try:
        payload = json.loads(snapshot_file(repo_path).read_text(encoding="utf-8"))
        return symbols_from_dict(payload.get("symbols", {}))
    except (OSError, json.JSONDecodeError):
        return {}


def save_snapshot(repo_path: str, all_symbols: Dict[str, List[Symbol]]) -> None:
    payload = {"version": 2, "symbols": symbols_to_dict(all_symbols)}
    try:
        snapshot_file(repo_path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def flatten_symbols(all_symbols: Dict[str, List[Symbol]]) -> Set[Tuple[str, str, str, int]]:
    return {(sym.file, sym.kind, sym.name, sym.line) for symbols in all_symbols.values() for sym in symbols}


def compare_symbol_snapshots(previous: Dict[str, List[Symbol]], current: Dict[str, List[Symbol]]) -> Dict[str, List[Dict[str, Any]]]:
    prev = flatten_symbols(previous)
    cur = flatten_symbols(current)
    added = cur - prev
    removed = prev - cur
    moved: List[Dict[str, Any]] = []
    prev_by_key = {(file, kind, name): line for file, kind, name, line in prev}
    cur_by_key = {(file, kind, name): line for file, kind, name, line in cur}
    for key, old_line in prev_by_key.items():
        new_line = cur_by_key.get(key)
        if new_line is not None and new_line != old_line:
            file, kind, name = key
            moved.append({"file": file, "kind": kind, "name": name, "from": old_line, "to": new_line})
    return {
        "added": [{"file": f, "kind": k, "name": n, "line": l} for f, k, n, l in sorted(added)],
        "removed": [{"file": f, "kind": k, "name": n, "line": l} for f, k, n, l in sorted(removed)],
        "moved": sorted(moved, key=lambda item: (item["file"], item["name"])),
    }


def read_text(repo_path: str, rel_path: str, max_bytes: int = MAX_FILE_SIZE) -> str:
    try:
        full_path = Path(repo_path) / rel_path
        if full_path.stat().st_size > max_bytes:
            return ""
        return full_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def read_json_file(repo_path: str, rel_path: str) -> Dict[str, Any]:
    text = read_text(repo_path, rel_path)
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def detect_frameworks(repo_path: str, files: List[str]) -> Dict[str, Any]:
    frameworks: Set[str] = set()
    entrypoints: Set[str] = set()
    scripts: Dict[str, str] = {}

    file_set = set(files)
    package = read_json_file(repo_path, "package.json") if "package.json" in file_set else {}
    if package:
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        scripts = {str(k): str(v) for k, v in package.get("scripts", {}).items()}
        for name, label in (
            ("next", "Next.js"), ("vite", "Vite"), ("react", "React"),
            ("vue", "Vue"), ("svelte", "Svelte"), ("express", "Express"),
            ("@nestjs/core", "NestJS"), ("electron", "Electron"),
        ):
            if name in deps:
                frameworks.add(label)
        for candidate in ("src/main.ts", "src/main.tsx", "src/index.ts", "src/index.tsx", "index.ts", "server.ts"):
            if candidate in file_set:
                entrypoints.add(candidate)

    if "pyproject.toml" in file_set and tomllib is not None:
        try:
            data = tomllib.loads(read_text(repo_path, "pyproject.toml"))
            raw_deps = data.get("project", {}).get("dependencies", [])
            deps_text = "\n".join(str(d).lower() for d in raw_deps)
            for name, label in (("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask"), ("pytest", "pytest")):
                if name in deps_text:
                    frameworks.add(label)
        except Exception:
            pass
    if "requirements.txt" in file_set:
        deps_text = read_text(repo_path, "requirements.txt").lower()
        for name, label in (("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask")):
            if name in deps_text:
                frameworks.add(label)
    for candidate in ("main.py", "app.py", "manage.py", "src/main.py"):
        if candidate in file_set:
            entrypoints.add(candidate)

    if "go.mod" in file_set:
        frameworks.add("Go module")
        for candidate in ("main.go", "cmd/main.go"):
            if candidate in file_set:
                entrypoints.add(candidate)
    if "Cargo.toml" in file_set:
        frameworks.add("Rust crate")
        for candidate in ("src/main.rs", "src/lib.rs"):
            if candidate in file_set:
                entrypoints.add(candidate)
    if any(Path(f).suffix in (".tf", ".tfvars", ".hcl") for f in files):
        frameworks.add("Terraform/HCL")
        for candidate in ("main.tf", "variables.tf", "outputs.tf"):
            if candidate in file_set:
                entrypoints.add(candidate)

    return {
        "frameworks": sorted(frameworks),
        "entrypoints": sorted(entrypoints),
        "package_scripts": scripts,
    }


IMPORT_RE = re.compile(r"(?:from\s+([\w\.\/\-@]+)\s+import|import\s+([\w\.\/\-@]+)|require\(['\"]([^'\"]+)['\"]\)|use\s+([\w:]+))")
FROM_STRING_RE = re.compile(r"\bfrom\s+['\"]([^'\"]+)['\"]")
SIDE_EFFECT_IMPORT_RE = re.compile(r"^import\s+['\"]([^'\"]+)['\"]")


def extract_imports(repo_path: str, rel_path: str) -> List[str]:
    text = read_text(repo_path, rel_path)
    if not text:
        return []
    ext = Path(rel_path).suffix
    imports: Set[str] = set()
    for line in text.splitlines()[:2000]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*")):
            continue
        if ext in (".js", ".ts", ".tsx"):
            match = FROM_STRING_RE.search(stripped) or SIDE_EFFECT_IMPORT_RE.search(stripped)
            if match:
                imports.add(match.group(1))
            require = re.search(r"require\(['\"]([^'\"]+)['\"]\)", stripped)
            if require:
                imports.add(require.group(1))
            continue
        if ext == ".py" and not stripped.startswith(("import ", "from ")):
            continue
        if ext == ".rs" and not stripped.startswith("use "):
            continue
        if ext == ".go" and not (stripped.startswith("import ") or stripped.startswith('"')):
            continue
        for match in IMPORT_RE.finditer(stripped):
            target = next((g for g in match.groups() if g), "")
            if target:
                imports.add(target)
    return sorted(imports)


def resolve_internal_import(import_name: str, importer: str, files: List[str]) -> Optional[str]:
    file_set = set(files)
    candidates: List[str] = []
    if import_name.startswith("."):
        base = Path(importer).parent / import_name
        candidates.extend(str(base.with_suffix(ext)).replace("\\", "/") for ext in SUPPORTED_EXTENSIONS)
        candidates.extend(str(base / f"index{ext}").replace("\\", "/") for ext in (".ts", ".tsx", ".js"))
    dotted = import_name.replace(".", "/").replace("::", "/")
    candidates.extend(f"{dotted}{ext}" for ext in SUPPORTED_EXTENSIONS)
    candidates.extend(f"src/{dotted}{ext}" for ext in SUPPORTED_EXTENSIONS)
    for candidate in candidates:
        normalized = os.path.normpath(candidate).replace("\\", "/")
        if normalized in file_set:
            return normalized
    return None


def dependency_graph(repo_path: str, files: List[str]) -> Dict[str, Any]:
    imports_by_file: Dict[str, List[str]] = {}
    imported_by: Dict[str, Set[str]] = defaultdict(set)
    external: Dict[str, int] = defaultdict(int)

    for rel_path in files:
        if Path(rel_path).suffix not in SUPPORTED_EXTENSIONS:
            continue
        imports = extract_imports(repo_path, rel_path)
        imports_by_file[rel_path] = imports
        for item in imports:
            internal = resolve_internal_import(item, rel_path, files)
            if internal:
                imported_by[internal].add(rel_path)
            else:
                root = item.split("/", 1)[0].split(".", 1)[0].split("::", 1)[0]
                if root and not root.startswith("."):
                    external[root] += 1

    internal_counts = {file: len(importers) for file, importers in imported_by.items()}
    return {
        "imports_by_file": imports_by_file,
        "internal_imported_by": {k: sorted(v) for k, v in imported_by.items()},
        "internal_counts": dict(sorted(internal_counts.items(), key=lambda item: (-item[1], item[0]))),
        "external_counts": dict(sorted(external.items(), key=lambda item: (-item[1], item[0]))),
    }


def pair_tests(files: List[str]) -> Dict[str, List[str]]:
    tests = [f for f in files if _is_test_file(f)]
    sources = [f for f in files if not _is_test_file(f) and Path(f).suffix in SUPPORTED_EXTENSIONS]
    pairs: Dict[str, List[str]] = defaultdict(list)
    for source in sources:
        stem = Path(source).stem
        source_parts = set(Path(source).parts)
        for test in tests:
            test_stem = Path(test).stem.replace(".test", "").replace(".spec", "")
            if stem == test_stem or stem in test or source_parts.intersection(Path(test).parts):
                pairs[source].append(test)
    return {k: sorted(v) for k, v in sorted(pairs.items()) if v}


def path_groups(files: List[str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = defaultdict(list)
    for rel_path in files:
        parts = rel_path.split("/")
        key = "(root)" if len(parts) == 1 else "/".join(parts[:2] if parts[0] in ("packages", "apps", "crates") else parts[:1])
        groups[key].append(rel_path)
    return dict(sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])))


def suggested_reads(all_symbols: Dict[str, List[Symbol]], files: List[str], limit: int = 5) -> List[str]:
    scored: List[Tuple[float, str]] = []
    for file_path, symbols in all_symbols.items():
        scored.append((sum(sym.importance for sym in symbols), file_path))
    if not scored:
        return files[:limit]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [file_path for _score, file_path in scored[:limit]]


def git_changed_files(repo_path: str, files: List[str]) -> List[str]:
    changed: Set[str] = set()
    commands = [["git", "diff", "--name-only", "HEAD"], ["git", "ls-files", "--others", "--exclude-standard"]]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                changed.update(line.strip() for line in result.stdout.splitlines() if line.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    file_set = set(files)
    return sorted(f for f in changed if f in file_set)


def health_report(repo_path: str, candidates: List[str], files: List[str], all_symbols: Dict[str, List[Symbol]], truncated: bool) -> Dict[str, Any]:
    large: List[str] = []
    parserless: Set[str] = set()
    for rel_path in files:
        try:
            if (Path(repo_path) / rel_path).stat().st_size > MAX_FILE_SIZE:
                large.append(rel_path)
        except OSError:
            pass
        ext = Path(rel_path).suffix
        if ext in SUPPORTED_EXTENSIONS and get_parser(ext) is None:
            parserless.add(ext)
    return {
        "tree_sitter_available": _TS_PACK_AVAILABLE,
        "source_candidates": len(candidates),
        "scanned_files": len(files),
        "truncated": truncated,
        "files_with_symbols": len(all_symbols),
        "large_files_skipped": large,
        "parserless_extensions": sorted(parserless),
        "cache_file": str(cache_file(repo_path)),
    }


def format_map(all_symbols: Dict[str, List[Symbol]], token_budget: int) -> str:
    if not all_symbols:
        return "# No symbols found"

    lines: List[str] = []

    file_scores: List[Tuple[str, float, List[Symbol]]] = []
    for file_path, symbols in all_symbols.items():
        if not symbols:
            continue
        total = sum(s.importance for s in symbols)
        file_scores.append((file_path, total, symbols))

    file_scores.sort(key=lambda x: (-x[1], x[0].startswith("tests/") or x[0].startswith("test_"), x[0]))

    char_budget = token_budget * 4
    current_chars = 0

    for file_path, _score, symbols in file_scores:
        symbols.sort(key=lambda s: (-s.importance, s.line))

        header = f"- {file_path}:"
        if current_chars + len(header) > char_budget:
            break

        lines.append(header)
        current_chars += len(header) + 1

        seen: Set[Tuple[str, str]] = set()
        for sym in symbols[:30]:
            key = (sym.kind, sym.name)
            if key in seen:
                continue
            seen.add(key)

            line_str = f"  {sym.kind} {sym.name} (line {sym.line})"
            if sym.refs > 0:
                line_str += f"  ← {sym.refs} files"

            if current_chars + len(line_str) > char_budget:
                break

            lines.append(line_str)
            current_chars += len(line_str) + 1

        if current_chars >= char_budget:
            break

    return "\n".join(lines)


def _append_with_budget(lines: List[str], text: str, current_chars: int, char_budget: int) -> int:
    if current_chars + len(text) + 1 > char_budget:
        return current_chars
    lines.append(text)
    return current_chars + len(text) + 1


def format_files(files: List[str], token_budget: int) -> str:
    if not files:
        return "# No source files found"

    char_budget = token_budget * 4
    current_chars = 0
    lines: List[str] = ["# Source file inventory"]
    current_chars += len(lines[0]) + 1

    grouped = {
        "Entry/config": [f for f in files if _is_config_or_entrypoint(f)],
        "Docs": [f for f in files if _is_doc_file(f)],
        "Source": [f for f in files if not _is_config_or_entrypoint(f) and not _is_doc_file(f) and not _is_test_file(f)],
        "Tests": [f for f in files if _is_test_file(f)],
    }

    for title, group in grouped.items():
        if not group:
            continue
        current_chars = _append_with_budget(lines, f"\n## {title} ({len(group)})", current_chars, char_budget)
        for rel_path in group[:80]:
            before = current_chars
            current_chars = _append_with_budget(lines, f"- {rel_path}", current_chars, char_budget)
            if current_chars == before:
                return "\n".join(lines)

    return "\n".join(lines)


def format_stats(
    files: List[str],
    all_symbols: Dict[str, List[Symbol]],
    candidate_count: int,
    truncated: bool,
) -> str:
    total_symbols = sum(len(symbols) for symbols in all_symbols.values())
    langs = language_stats(files)
    lines = [
        "# Repo Baby stats",
        f"- source candidates: {candidate_count}",
        f"- scanned files: {len(files)}" + (" (capped)" if truncated else ""),
        f"- files with symbols: {len(all_symbols)}",
        f"- symbols: {total_symbols}",
    ]
    if langs:
        lines.append("- languages: " + ", ".join(f"{lang} {count}" for lang, count in langs.items()))
    if truncated:
        lines.append("- note: increase --max-files or narrow --scope for deeper coverage")
    if not _TS_PACK_AVAILABLE:
        lines.append("- warning: tree-sitter-language-pack is not installed; symbol extraction is unavailable")
    return "\n".join(lines)


def format_search(
    files: List[str],
    all_symbols: Dict[str, List[Symbol]],
    query: str,
    token_budget: int,
) -> str:
    query_l = query.lower().strip()
    if not query_l:
        return "# Search requires --query"

    char_budget = token_budget * 4
    current_chars = 0
    lines: List[str] = [f"# Search: {query}"]
    current_chars += len(lines[0]) + 1

    symbol_matches: List[Symbol] = []
    for symbols in all_symbols.values():
        for sym in symbols:
            if query_l in sym.name.lower() or query_l in sym.kind.lower():
                symbol_matches.append(sym)
    symbol_matches.sort(key=lambda s: (-s.importance, s.file, s.line))

    file_matches = [f for f in files if query_l in f.lower()]

    if symbol_matches:
        current_chars = _append_with_budget(lines, "\n## Symbols", current_chars, char_budget)
        for sym in symbol_matches[:80]:
            ref = f"  ← {sym.refs} files" if sym.refs > 0 else ""
            before = current_chars
            current_chars = _append_with_budget(
                lines,
                f"- {sym.file}: {sym.kind} {sym.name} (line {sym.line}){ref}",
                current_chars,
                char_budget,
            )
            if current_chars == before:
                return "\n".join(lines)

    if file_matches:
        current_chars = _append_with_budget(lines, "\n## Files", current_chars, char_budget)
        for rel_path in file_matches[:100]:
            before = current_chars
            current_chars = _append_with_budget(lines, f"- {rel_path}", current_chars, char_budget)
            if current_chars == before:
                return "\n".join(lines)

    if not symbol_matches and not file_matches:
        lines.append("No matches")

    return "\n".join(lines)


def format_suggestions(reads: List[str]) -> str:
    if not reads:
        return ""
    lines = ["\n## Suggested next reads"]
    lines.extend(f"{idx}. {path}" for idx, path in enumerate(reads, start=1))
    return "\n".join(lines)


def format_overview(meta: Dict[str, Any], token_budget: int) -> str:
    char_budget = token_budget * 4
    current_chars = 0
    lines: List[str] = ["# Repo overview"]
    current_chars += len(lines[0]) + 1

    frameworks = meta.get("frameworks", {}).get("frameworks", [])
    entrypoints = meta.get("frameworks", {}).get("entrypoints", [])
    if frameworks:
        current_chars = _append_with_budget(lines, "- detected: " + ", ".join(frameworks), current_chars, char_budget)
    if entrypoints:
        current_chars = _append_with_budget(lines, "- likely entrypoints: " + ", ".join(entrypoints[:8]), current_chars, char_budget)

    stats = meta.get("stats", {})
    current_chars = _append_with_budget(
        lines,
        f"- files: {stats.get('scanned_files', 0)} scanned / {stats.get('source_candidates', 0)} candidates; symbols: {stats.get('symbols', 0)}",
        current_chars,
        char_budget,
    )
    langs = stats.get("languages", {})
    if langs:
        current_chars = _append_with_budget(lines, "- languages: " + ", ".join(f"{k} {v}" for k, v in langs.items()), current_chars, char_budget)

    reads = meta.get("suggested_reads", [])
    if reads:
        current_chars = _append_with_budget(lines, "\n## Suggested next reads", current_chars, char_budget)
        for idx, path in enumerate(reads, start=1):
            current_chars = _append_with_budget(lines, f"{idx}. {path}", current_chars, char_budget)

    scripts = meta.get("frameworks", {}).get("package_scripts", {})
    if scripts:
        current_chars = _append_with_budget(lines, "\n## Package scripts", current_chars, char_budget)
        for name, command in list(scripts.items())[:12]:
            current_chars = _append_with_budget(lines, f"- {name}: {command}", current_chars, char_budget)

    return "\n".join(lines)


def format_deps(graph: Dict[str, Any], token_budget: int) -> str:
    char_budget = token_budget * 4
    current_chars = 0
    lines = ["# Dependency graph summary"]
    current_chars += len(lines[0]) + 1
    internal = graph.get("internal_counts", {})
    external = graph.get("external_counts", {})
    if internal:
        current_chars = _append_with_budget(lines, "\n## Internal hotspots", current_chars, char_budget)
        for file_path, count in list(internal.items())[:30]:
            current_chars = _append_with_budget(lines, f"- {file_path}  ← imported by {count} files", current_chars, char_budget)
    if external:
        current_chars = _append_with_budget(lines, "\n## External packages", current_chars, char_budget)
        for pkg, count in list(external.items())[:40]:
            current_chars = _append_with_budget(lines, f"- {pkg}  ← {count} imports", current_chars, char_budget)
    if not internal and not external:
        lines.append("No imports detected")
    return "\n".join(lines)


def format_pairs(pairs: Dict[str, List[str]], token_budget: int) -> str:
    if not pairs:
        return "# Test/source pairs\nNo likely pairs found"
    char_budget = token_budget * 4
    current_chars = 0
    lines = ["# Test/source pairs"]
    current_chars += len(lines[0]) + 1
    for source, tests in pairs.items():
        before = current_chars
        current_chars = _append_with_budget(lines, f"- {source}", current_chars, char_budget)
        for test in tests[:8]:
            current_chars = _append_with_budget(lines, f"  - {test}", current_chars, char_budget)
        if current_chars == before:
            break
    return "\n".join(lines)


def format_groups(groups: Dict[str, List[str]], token_budget: int) -> str:
    char_budget = token_budget * 4
    current_chars = 0
    lines = ["# Path groups"]
    current_chars += len(lines[0]) + 1
    for group, group_files in groups.items():
        current_chars = _append_with_budget(lines, f"\n## {group} ({len(group_files)})", current_chars, char_budget)
        for rel_path in group_files[:40]:
            before = current_chars
            current_chars = _append_with_budget(lines, f"- {rel_path}", current_chars, char_budget)
            if current_chars == before:
                return "\n".join(lines)
    return "\n".join(lines)


def format_changed(changed_files: List[str], snapshot_diff: Dict[str, List[Dict[str, Any]]], all_symbols: Dict[str, List[Symbol]], token_budget: int) -> str:
    char_budget = token_budget * 4
    current_chars = 0
    lines = ["# Changed files and symbols"]
    current_chars += len(lines[0]) + 1
    if changed_files:
        current_chars = _append_with_budget(lines, "\n## Git changed files", current_chars, char_budget)
        for rel_path in changed_files[:80]:
            current_chars = _append_with_budget(lines, f"- {rel_path}", current_chars, char_budget)
            for sym in all_symbols.get(rel_path, [])[:12]:
                current_chars = _append_with_budget(lines, f"  {sym.kind} {sym.name} (line {sym.line})", current_chars, char_budget)
    else:
        current_chars = _append_with_budget(lines, "No git changes detected", current_chars, char_budget)

    if any(snapshot_diff.values()):
        current_chars = _append_with_budget(lines, "\n## Symbol changes since last Repo Baby snapshot", current_chars, char_budget)
        for title in ("added", "removed", "moved"):
            items = snapshot_diff.get(title, [])
            if not items:
                continue
            current_chars = _append_with_budget(lines, f"### {title}", current_chars, char_budget)
            for item in items[:40]:
                if title == "moved":
                    text = f"- {item['file']}: {item['kind']} {item['name']} line {item['from']} → {item['to']}"
                else:
                    text = f"- {item['file']}: {item['kind']} {item['name']} (line {item['line']})"
                current_chars = _append_with_budget(lines, text, current_chars, char_budget)
    return "\n".join(lines)


def symbol_detail(repo_path: str, all_symbols: Dict[str, List[Symbol]], query: str) -> List[Dict[str, Any]]:
    query_l = query.lower().strip()
    if not query_l:
        return []
    matches: List[Symbol] = []
    for symbols in all_symbols.values():
        for sym in symbols:
            if query_l in sym.name.lower():
                matches.append(sym)
    matches.sort(key=lambda s: (-s.importance, s.file, s.line))
    details: List[Dict[str, Any]] = []
    for sym in matches[:20]:
        lines = read_text(repo_path, sym.file).splitlines()
        start = max(0, sym.line - 4)
        end = min(len(lines), sym.line + 8)
        context = lines[start:end]
        signature = lines[sym.line - 1].strip() if 0 <= sym.line - 1 < len(lines) else ""
        decorators: List[str] = []
        cursor = sym.line - 2
        while cursor >= 0 and lines[cursor].strip().startswith("@"):
            decorators.insert(0, lines[cursor].strip())
            cursor -= 1
        doc = ""
        for candidate in context[1:5]:
            stripped = candidate.strip()
            if stripped.startswith(('"""', "'''", "//", "#", "*")):
                doc = stripped.strip('"\'#/ *')
                break
        details.append({**sym.to_dict(), "signature": signature, "decorators": decorators, "doc": doc})
    return details


def format_detail(details: List[Dict[str, Any]], query: str, token_budget: int) -> str:
    if not query.strip():
        return "# Symbol detail requires --query"
    if not details:
        return f"# Symbol detail: {query}\nNo matches"
    char_budget = token_budget * 4
    current_chars = 0
    lines = [f"# Symbol detail: {query}"]
    current_chars += len(lines[0]) + 1
    for item in details:
        current_chars = _append_with_budget(lines, f"\n- {item['file']}: {item['kind']} {item['name']} (line {item['line']})", current_chars, char_budget)
        if item.get("signature"):
            current_chars = _append_with_budget(lines, f"  signature: {item['signature']}", current_chars, char_budget)
        if item.get("decorators"):
            current_chars = _append_with_budget(lines, "  decorators: " + ", ".join(item["decorators"]), current_chars, char_budget)
        if item.get("doc"):
            current_chars = _append_with_budget(lines, f"  doc: {item['doc']}", current_chars, char_budget)
    return "\n".join(lines)


def format_health(report: Dict[str, Any]) -> str:
    lines = ["# Repo Baby health"]
    lines.append(f"- tree-sitter: {'ok' if report['tree_sitter_available'] else 'missing'}")
    lines.append(f"- source candidates: {report['source_candidates']}")
    lines.append(f"- scanned files: {report['scanned_files']}" + (" (capped)" if report.get("truncated") else ""))
    lines.append(f"- files with symbols: {report['files_with_symbols']}")
    if report.get("large_files_skipped"):
        lines.append("- large files skipped: " + ", ".join(report["large_files_skipped"][:20]))
    if report.get("parserless_extensions"):
        lines.append("- parserless extensions: " + ", ".join(report["parserless_extensions"]))
    lines.append(f"- cache: {report['cache_file']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Repo Baby — repository map generator")
    ap.add_argument("--path", required=True, help="Path to repository root")
    ap.add_argument("--scope", default=".", help="Limit to a subdirectory (default: .)")
    ap.add_argument("--token-budget", type=int, default=800,
                    help="Approximate token budget for the output (default: 800)")
    ap.add_argument("--mode", choices=(
        "map", "overview", "files", "stats", "search", "changed", "deps",
        "pairs", "detail", "groups", "health",
    ), default="map", help="Output mode")
    ap.add_argument("--format", choices=("text", "json"), default="text",
                    help="Output format (default: text)")
    ap.add_argument("--query", default="", help="Search/detail query")
    ap.add_argument("--max-files", type=int, default=MAX_FILES_DEFAULT,
                    help=f"Maximum source files to scan (default: {MAX_FILES_DEFAULT})")
    ap.add_argument("--no-cache", action="store_true", help="Disable symbol cache")
    args = ap.parse_args()

    repo_path = os.path.abspath(args.path)

    if not os.path.isdir(repo_path):
        print(f"Error: {repo_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    candidates = prioritize_files(discover_files(repo_path, args.scope))
    if not candidates:
        if args.format == "json":
            print(json.dumps({"mode": args.mode, "files": [], "error": "No source files found"}, indent=2))
        else:
            print("# No source files found")
        sys.exit(0)

    max_files = max(1, args.max_files)
    files = candidates[:max_files]
    truncated = len(candidates) > len(files)

    graph = dependency_graph(repo_path, files) if args.mode in ("deps", "overview", "map", "stats", "health") else {}
    frameworks = detect_frameworks(repo_path, files)
    pairs = pair_tests(files) if args.mode in ("pairs", "overview") else {}
    groups = path_groups(files) if args.mode in ("groups", "overview") else {}

    needs_symbols = args.mode in ("map", "overview", "stats", "search", "changed", "detail", "health")
    all_symbols: Dict[str, List[Symbol]] = {}
    cache_hit = False

    if needs_symbols and _TS_PACK_AVAILABLE:
        max_files = max(1, args.max_files)
        if not args.no_cache:
            cached = load_cached_symbols(repo_path, files, args.scope, max_files)
            if cached is not None:
                all_symbols = cached
                cache_hit = True
        if not all_symbols:
            for rel_path in files:
                symbols = extract_symbols(rel_path, repo_path)
                if symbols:
                    all_symbols[rel_path] = symbols
            if not args.no_cache:
                save_cached_symbols(repo_path, files, args.scope, max_files, all_symbols)

    if all_symbols:
        compute_importance(all_symbols, repo_path, graph.get("internal_counts", {}))

    stats_data = {
        "source_candidates": len(candidates),
        "scanned_files": len(files),
        "truncated": truncated,
        "files_with_symbols": len(all_symbols),
        "symbols": sum(len(symbols) for symbols in all_symbols.values()),
        "languages": language_stats(files),
        "cache_hit": cache_hit,
    }
    reads = suggested_reads(all_symbols, files)
    snapshot_diff = compare_symbol_snapshots(load_snapshot(repo_path), all_symbols) if all_symbols else {"added": [], "removed": [], "moved": []}
    changed_files = git_changed_files(repo_path, files) if args.mode == "changed" else []
    health = health_report(repo_path, candidates, files, all_symbols, truncated) if args.mode == "health" else {}
    detail = symbol_detail(repo_path, all_symbols, args.query) if args.mode == "detail" else []

    data: Dict[str, Any] = {
        "mode": args.mode,
        "scope": normalize_scope(args.scope),
        "stats": stats_data,
        "frameworks": frameworks,
        "suggested_reads": reads,
    }

    if args.mode == "files":
        data["files"] = files
        output = format_files(files, args.token_budget)
    elif args.mode == "stats":
        data["symbols"] = symbols_to_dict(all_symbols)
        output = format_stats(files, all_symbols, len(candidates), truncated)
    elif args.mode == "search":
        data["symbols"] = symbols_to_dict(all_symbols)
        output = format_search(files, all_symbols, args.query, args.token_budget)
    elif args.mode == "changed":
        data.update({"changed_files": changed_files, "symbol_diff": snapshot_diff, "symbols": symbols_to_dict(all_symbols)})
        output = format_changed(changed_files, snapshot_diff, all_symbols, args.token_budget)
    elif args.mode == "deps":
        data["dependency_graph"] = graph
        output = format_deps(graph, args.token_budget)
    elif args.mode == "pairs":
        data["pairs"] = pairs
        output = format_pairs(pairs, args.token_budget)
    elif args.mode == "detail":
        data["detail"] = detail
        output = format_detail(detail, args.query, args.token_budget)
    elif args.mode == "groups":
        data["groups"] = groups
        output = format_groups(groups, args.token_budget)
    elif args.mode == "health":
        data["health"] = health
        output = format_health(health)
    elif args.mode == "overview":
        data.update({"dependency_graph": graph, "pairs": pairs, "groups": groups})
        output = format_overview(data, args.token_budget)
    else:
        data["symbols"] = symbols_to_dict(all_symbols)
        output = format_map(all_symbols, args.token_budget)
        suggestions = format_suggestions(reads)
        if suggestions:
            output += suggestions

    if needs_symbols and not _TS_PACK_AVAILABLE and args.mode in ("map", "search", "detail"):
        output = "# Tree-sitter dependencies missing\nRun `/repo-baby doctor` in Pi or `npm run install-deps` in the extension directory."
        data["error"] = "Tree-sitter dependencies missing"
    elif needs_symbols and not all_symbols and args.mode not in ("health", "overview"):
        if args.mode in ("map", "search", "detail"):
            output = "# No symbols found in source files\n\nTry `--mode files` for a file inventory, or narrow `--scope` to source code."

    if args.format == "json":
        print(json.dumps(data, indent=2))
    else:
        print(output)

    if all_symbols:
        save_snapshot(repo_path, all_symbols)


if __name__ == "__main__":
    main()
