"""Static, bounded, target-centered source context selection.

Only paths already reported by a ProjectProfile are indexed. No target module is
imported or executed, and no environment is needed.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import heapq
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from autotest.project_inspector import ProjectProfile, PythonModuleInfo

_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")
_BUILTINS = frozenset(dir(builtins))


class ContextSelectionError(Exception):
    """Invalid target or infrastructure prevents reliable static selection."""


@dataclass(frozen=True, slots=True)
class ContextTarget:
    file: Path
    function: str

    @classmethod
    def parse(cls, value: str) -> ContextTarget:
        if value.count(":") != 1:
            raise ContextSelectionError("Context target must be <relative-python-file>:<function>.")
        raw_file, function = value.split(":")
        file = Path(raw_file)
        if (
            not raw_file
            or not _IDENTIFIER.fullmatch(function)
            or file.is_absolute()
            or file.suffix != ".py"
            or ".." in file.parts
        ):
            raise ContextSelectionError("Invalid project-relative Python context target.")
        return cls(file, function)


@dataclass(frozen=True, slots=True)
class ContextSelectionPolicy:
    max_chars: int = 32_000
    max_files: int = 8
    max_items: int = 24
    max_dependency_depth: int = 2

    def __post_init__(self) -> None:
        if min(self.max_chars, self.max_files, self.max_items) < 1 or self.max_dependency_depth < 0:
            raise ValueError("Context limits must be positive; depth may be zero.")


@dataclass(frozen=True, slots=True)
class ContextItem:
    path: str
    kind: str
    symbol: str | None
    start_line: int
    end_line: int
    depth: int
    reason: str
    source: str
    source_sha256: str

    @property
    def identity(self) -> str:
        return f"{self.path}:{self.start_line}:{self.symbol or '<import>'}"


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    from_item: str
    to_item: str
    reference: str
    kind: str
    depth: int


@dataclass(frozen=True, slots=True)
class ExternalReference:
    module: str
    reference: str
    import_source: str
    classification: str


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    reference: str
    reason: str
    from_item: str


@dataclass(frozen=True, slots=True)
class ContextOmission:
    path: str
    symbol: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ContextBundle:
    project_profile_sha256: str
    target: ContextTarget
    policy: ContextSelectionPolicy
    target_source_sha256: str
    items: tuple[ContextItem, ...]
    dependency_edges: tuple[DependencyEdge, ...]
    external_references: tuple[ExternalReference, ...]
    unresolved_references: tuple[UnresolvedReference, ...]
    omitted_items: tuple[ContextOmission, ...]
    warnings: tuple[str, ...]
    selected_file_sha256: tuple[tuple[str, str], ...]
    direct_local_dependencies: int
    recursive_local_dependencies: int
    maximum_dependency_depth_reached: int
    bundle_sha256: str

    @property
    def context_chars(self) -> int:
        return sum(len(item.source) for item in self.items)

    @property
    def status(self) -> str:
        return "PARTIAL" if self.unresolved_references or self.omitted_items else "COMPLETE"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "project_profile_sha256": self.project_profile_sha256,
            "target": {"file": self.target.file.as_posix(), "function": self.target.function},
            "policy": asdict(self.policy),
            "target_source_sha256": self.target_source_sha256,
            "items": [asdict(item) for item in self.items],
            "dependency_edges": [asdict(edge) for edge in self.dependency_edges],
            "external_references": [asdict(item) for item in self.external_references],
            "unresolved_references": [asdict(item) for item in self.unresolved_references],
            "omitted_items": [asdict(item) for item in self.omitted_items],
            "warnings": list(self.warnings),
            "selected_file_sha256": dict(self.selected_file_sha256),
            "selected_file_count": len(self.selected_file_sha256),
            "selected_item_count": len(self.items),
            "target_source_chars": len(self.items[0].source),
            "max_context_chars": self.policy.max_chars,
            "context_chars": self.context_chars,
            "direct_local_dependencies": self.direct_local_dependencies,
            "recursive_local_dependencies": self.recursive_local_dependencies,
            "maximum_dependency_depth_reached": self.maximum_dependency_depth_reached,
            "status": self.status,
        }
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        payload["bundle_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def render(self) -> str:
        target = self.items[0]
        lines = [
            "TARGET",
            f"File: {target.path}",
            f"Symbol: {target.symbol}",
            f"Lines: {target.start_line}-{target.end_line}",
            "",
            target.source.rstrip("\n"),
            "",
            "RELEVANT IMPORTS",
        ]
        for item in self.items[1:]:
            if item.kind == "IMPORT":
                lines.extend((f"{item.path}:{item.start_line}", item.source.rstrip("\n")))
        lines.extend(("", "SUPPORTING CONTEXT"))
        for number, item in enumerate((i for i in self.items[1:] if i.kind != "IMPORT"), 1):
            lines.extend(
                (
                    f"[{number}]",
                    f"File: {item.path}",
                    f"Symbol: {item.symbol}",
                    f"Lines: {item.start_line}-{item.end_line}",
                    f"Reason: {item.reason}",
                    f"Depth: {item.depth}",
                    "",
                    item.source.rstrip("\n"),
                    "",
                )
            )
        lines.append("EXTERNAL REFERENCES")
        lines.extend(
            f"{item.classification}: {item.reference} via {item.import_source}"
            for item in self.external_references
        )
        lines.append("UNRESOLVED REFERENCES")
        lines.extend(f"{item.reference}: {item.reason}" for item in self.unresolved_references)
        lines.append("OMITTED CONTEXT")
        lines.extend(f"{item.path}:{item.symbol}: {item.reason}" for item in self.omitted_items)
        return "\n".join(lines) + "\n"


@dataclass(slots=True)
class _Module:
    info: PythonModuleInfo
    path: str
    source: str
    lines: list[str]
    tree: ast.Module
    symbols: dict[str, list[ast.stmt]]
    imports: list[ast.Import | ast.ImportFrom]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _start(node: ast.stmt) -> int:
    decorators = getattr(node, "decorator_list", ())
    return min([node.lineno, *(d.lineno for d in decorators)])


def _bindings(node: ast.stmt) -> tuple[str, ...]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (node.name,)
    if isinstance(node, ast.Assign):
        return tuple(
            n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)
        )
    if isinstance(node, ast.AnnAssign):
        return tuple(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
    return ()


def _imports(nodes: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        elif isinstance(node, (ast.If, ast.Try, ast.TryStar)):
            bodies = [node.body, node.orelse]
            if isinstance(node, (ast.Try, ast.TryStar)):
                bodies.extend([node.finalbody, *(handler.body for handler in node.handlers)])
            for body in bodies:
                found.extend(_imports(body))
    return found


class _References(ast.NodeVisitor):
    """Conservative free-name and attribute-chain collection for one declaration."""

    def __init__(self, node: ast.stmt) -> None:
        self.locals: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            self.locals.update(arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs])
            if args.vararg:
                self.locals.add(args.vararg.arg)
            if args.kwarg:
                self.locals.add(args.kwarg.arg)
            for part in node.body:
                self._find_locals(part)
        self.names: set[str] = set()
        self.chains: set[str] = set()
        self.visit(node)

    def _find_locals(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.locals.add(node.name)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.locals.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            self.locals.add(node.name)
        for child in ast.iter_child_nodes(node):
            self._find_locals(child)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for part in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if part is not None:
                self.visit(part)
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if arg.annotation:
                self.visit(arg.annotation)
        for arg in (node.args.vararg, node.args.kwarg):
            if arg and arg.annotation:
                self.visit(arg.annotation)
        if node.returns:
            self.visit(node.returns)
        for part in node.body:
            if not isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(part)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for part in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(part)
        for part in node.body:
            if not isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(part)

    def visit_Name(self, node: ast.Name) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and node.id not in self.locals
            and node.id not in _BUILTINS
        ):
            self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = [node.attr]
        root = node.value
        while isinstance(root, ast.Attribute):
            parts.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id not in self.locals:
            self.chains.add(".".join([root.id, *reversed(parts)]))
        self.generic_visit(node)


class ContextSelector:
    """Resolve only statically visible, locally indexed declarations."""

    def __init__(self, *, max_source_file_bytes: int = 2 * 1024 * 1024) -> None:
        self.max_source_file_bytes = max_source_file_bytes

    def select(
        self,
        project_root: Path,
        profile: ProjectProfile,
        target: ContextTarget,
        policy: ContextSelectionPolicy = ContextSelectionPolicy(),
    ) -> ContextBundle:
        root = Path(project_root).resolve(strict=True)
        # Profile paths are structural inputs; their project-relative form also
        # applies to an unchanged Phase 5B source copy at a different root.
        relative = target.file.as_posix().replace("\\", "/")
        target = ContextTarget(Path(relative), target.function)
        if (
            target.file.is_absolute()
            or ".." in target.file.parts
            or target.file.suffix != ".py"
            or not _IDENTIFIER.fullmatch(target.function)
        ):
            raise ContextSelectionError("Invalid project-relative Python context target.")
        by_path = {
            item.path.relative_to(profile.root).as_posix(): item
            for item in profile.modules
            if not item.is_test_module
        }
        info = by_path.get(relative)
        if info is None:
            raise ContextSelectionError("Target is absent from production ProjectProfile modules.")
        by_name: dict[str, list[str]] = {}
        for path, item in sorted(by_path.items()):
            if item.module_name:
                by_name.setdefault(item.module_name, []).append(path)
        cache: dict[str, _Module | None] = {}
        warnings: set[str] = set()

        def module(path: str, *, mandatory: bool = False) -> _Module | None:
            if path in cache:
                return cache[path]
            file = root / path
            try:
                for candidate_path in (file, *file.parents):
                    if candidate_path == root:
                        break
                    if candidate_path.is_symlink() or (
                        hasattr(candidate_path, "is_junction") and candidate_path.is_junction()
                    ):
                        raise ValueError("symlink/junction")
                if (
                    not file.resolve().is_relative_to(root)
                    or file.stat().st_size > self.max_source_file_bytes
                ):
                    raise ValueError("outside root or oversized source")
                raw = file.read_bytes()
                if len(raw) > self.max_source_file_bytes:
                    raise ValueError("oversized source")
                source = raw.decode("utf-8")
                tree = ast.parse(source, filename=path)
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                if mandatory:
                    raise ContextSelectionError(
                        f"Cannot parse target module {path}: {exc}"
                    ) from exc
                warnings.add(f"Cannot parse supporting module {path}: {type(exc).__name__}")
                cache[path] = None
                return None
            symbols: dict[str, list[ast.stmt]] = {}
            for node in tree.body:
                for name in _bindings(node):
                    symbols.setdefault(name, []).append(node)
            result = _Module(
                by_path[path],
                path,
                source,
                source.splitlines(keepends=True),
                tree,
                symbols,
                _imports(tree.body),
            )
            cache[path] = result
            return result

        target_module = module(relative, mandatory=True)
        assert target_module is not None
        declarations = [
            item
            for item in target_module.symbols.get(target.function, [])
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if len(declarations) != 1 or len(target_module.symbols.get(target.function, [])) != 1:
            raise ContextSelectionError(
                "Target function is missing or ambiguously redeclared at top level."
            )

        def item_for(
            mod: _Module, node: ast.stmt, depth: int, reason: str, *, name: str | None = None
        ) -> ContextItem:
            start = _start(node)
            source = "".join(mod.lines[start - 1 : node.end_lineno])
            kind = (
                "IMPORT"
                if isinstance(node, (ast.Import, ast.ImportFrom))
                else "ASYNC_FUNCTION"
                if isinstance(node, ast.AsyncFunctionDef)
                else "FUNCTION"
                if isinstance(node, ast.FunctionDef)
                else "CLASS"
                if isinstance(node, ast.ClassDef)
                else "CONSTANT_OR_ASSIGNMENT"
            )
            return ContextItem(
                mod.path, kind, name, start, node.end_lineno, depth, reason, source, _sha(source)
            )

        first = item_for(target_module, declarations[0], 0, "selected target", name=target.function)
        if len(first.source) > policy.max_chars:
            raise ContextSelectionError("TARGET_EXCEEDS_CONTEXT_BUDGET")
        selected: dict[str, ContextItem] = {first.identity: first}
        selected_files = {relative}
        edges: set[DependencyEdge] = set()
        external: set[ExternalReference] = set()
        unresolved: set[UnresolvedReference] = set()
        omitted: set[ContextOmission] = set()
        queue: list[tuple[tuple[int, int, str, int, str], int, _Module, ast.stmt, ContextItem]] = []
        serial = 0

        def enqueue(
            mod: _Module,
            node: ast.stmt,
            depth: int,
            reason: str,
            priority: int,
            parent: ContextItem,
            reference: str,
            kind: str,
            *,
            name: str | None = None,
        ) -> None:
            nonlocal serial
            candidate = item_for(mod, node, depth, reason, name=name)
            edges.add(DependencyEdge(parent.identity, candidate.identity, reference, kind, depth))
            if depth > policy.max_dependency_depth:
                omitted.add(ContextOmission(candidate.path, candidate.symbol, "DEPTH_LIMIT"))
                return
            if candidate.identity in selected:
                return
            serial += 1
            key = (depth, priority, candidate.path, candidate.start_line, candidate.symbol or "")
            heapq.heappush(queue, (key, serial, mod, node, candidate))

        def inspect(mod: _Module, node: ast.stmt, parent: ContextItem) -> None:
            refs = _References(node)
            imports: dict[str, tuple[ast.Import | ast.ImportFrom, str, str | None]] = {}
            stars: list[ast.ImportFrom] = []
            for imp in mod.imports:
                if isinstance(imp, ast.Import):
                    for alias in imp.names:
                        imports[alias.asname or alias.name.split(".")[0]] = (imp, alias.name, None)
                else:
                    base = self._import_base(mod.info.module_name, mod.path, imp)
                    for alias in imp.names:
                        if alias.name == "*":
                            stars.append(imp)
                        else:
                            imports[alias.asname or alias.name] = (imp, base, alias.name)
            for star in stars:
                unresolved.add(UnresolvedReference("*", "STAR_IMPORT_UNRESOLVED", parent.identity))
            for name in sorted(refs.names):
                declarations = mod.symbols.get(name, [])
                if declarations:
                    if len(declarations) != 1:
                        unresolved.add(
                            UnresolvedReference(name, "AMBIGUOUS_LOCAL_SYMBOL", parent.identity)
                        )
                    else:
                        enqueue(
                            mod,
                            declarations[0],
                            parent.depth + 1,
                            "same-module dependency",
                            1,
                            parent,
                            name,
                            "SAME_MODULE_SYMBOL",
                            name=name,
                        )
                    continue
                if name not in imports:
                    unresolved.add(UnresolvedReference(name, "UNBOUND_GLOBAL", parent.identity))
                    continue
                imp, base, imported = imports[name]
                enqueue(
                    mod, imp, parent.depth, "relevant import", 0, parent, name, "IMPORT_EVIDENCE"
                )
                if not base:
                    unresolved.add(
                        UnresolvedReference(name, "RELATIVE_IMPORT_UNRESOLVED", parent.identity)
                    )
                    continue
                module_name = base
                symbols: set[str] = set()
                if imported is None:
                    prefix = base if name == base.split(".")[0] else name
                    symbols = {
                        chain[len(prefix) + 1 :].split(".")[0]
                        for chain in refs.chains
                        if chain.startswith(prefix + ".")
                    }
                elif f"{base}.{imported}" in by_name and not any(
                    imported in (candidate.symbols if candidate else {})
                    for candidate in (module(path) for path in by_name.get(base, []))
                ):
                    module_name = f"{base}.{imported}"
                    symbols = {
                        chain[len(name) + 1 :].split(".")[0]
                        for chain in refs.chains
                        if chain.startswith(name + ".")
                    }
                else:
                    symbols = {imported}
                paths = by_name.get(module_name, [])
                if len(paths) > 1:
                    unresolved.add(
                        UnresolvedReference(name, "AMBIGUOUS_LOCAL_MODULE", parent.identity)
                    )
                elif len(paths) == 1:
                    support = module(paths[0])
                    if support is None:
                        unresolved.add(
                            UnresolvedReference(name, "SUPPORT_MODULE_UNREADABLE", parent.identity)
                        )
                    elif not symbols:
                        unresolved.add(
                            UnresolvedReference(name, "MODULE_OBJECT_NO_SYMBOL", parent.identity)
                        )
                    else:
                        for symbol in sorted(symbols):
                            if len(support.symbols.get(symbol, [])) == 1:
                                kind = (
                                    "IMPORTED_SYMBOL"
                                    if imported and module_name == base
                                    else "IMPORTED_MODULE_ATTRIBUTE"
                                )
                                enqueue(
                                    support,
                                    support.symbols[symbol][0],
                                    parent.depth + 1,
                                    "imported local dependency",
                                    2 if kind == "IMPORTED_SYMBOL" else 3,
                                    parent,
                                    f"{name}.{symbol}"
                                    if kind == "IMPORTED_MODULE_ATTRIBUTE"
                                    else name,
                                    kind,
                                    name=symbol,
                                )
                            else:
                                unresolved.add(
                                    UnresolvedReference(
                                        f"{name}.{symbol}",
                                        "MISSING_OR_AMBIGUOUS_LOCAL_SYMBOL",
                                        parent.identity,
                                    )
                                )
                else:
                    classification = (
                        "STDLIB"
                        if module_name.split(".")[0] in sys.stdlib_module_names
                        else "THIRD_PARTY_OR_UNKNOWN"
                    )
                    external.add(
                        ExternalReference(
                            module_name,
                            name,
                            item_for(mod, imp, parent.depth, "", name=None).source.rstrip("\n"),
                            classification,
                        )
                    )
            dynamic_call = any(
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Name)
                and part.func.id in ("__import__", "getattr", "globals", "locals")
                for part in ast.walk(node)
            )
            if dynamic_call or any(c.startswith("importlib.import_module") for c in refs.chains):
                unresolved.add(
                    UnresolvedReference(
                        "dynamic lookup", "DYNAMIC_REFERENCE_UNRESOLVED", parent.identity
                    )
                )

        inspect(target_module, declarations[0], first)
        while queue:
            _, _, mod, node, candidate = heapq.heappop(queue)
            if candidate.identity in selected:
                continue
            reason = (
                "ITEM_BUDGET"
                if len(selected) >= policy.max_items
                else "FILE_BUDGET"
                if candidate.path not in selected_files and len(selected_files) >= policy.max_files
                else "CHARACTER_BUDGET"
                if sum(len(i.source) for i in selected.values()) + len(candidate.source)
                > policy.max_chars
                else None
            )
            if reason:
                omitted.add(ContextOmission(candidate.path, candidate.symbol, reason))
                continue
            selected[candidate.identity] = candidate
            selected_files.add(candidate.path)
            if candidate.kind != "IMPORT" and candidate.depth < policy.max_dependency_depth:
                inspect(mod, node, candidate)
            elif candidate.kind != "IMPORT" and candidate.depth == policy.max_dependency_depth:
                inspect(mod, node, candidate)
        items = (
            first,
            *sorted(
                (i for i in selected.values() if i.identity != first.identity),
                key=lambda i: (
                    i.depth,
                    0 if i.kind == "IMPORT" else 1,
                    i.path,
                    i.start_line,
                    i.symbol or "",
                ),
            ),
        )
        files = tuple(
            (path, _sha(module(path).source))
            for path in sorted(selected_files)
            if module(path) is not None
        )
        edge_tuple = tuple(
            sorted(
                (e for e in edges if e.to_item in selected or e.to_item == first.identity),
                key=lambda e: (e.depth, e.from_item, e.to_item, e.reference, e.kind),
            )
        )
        direct = sum(i.depth == 1 and i.kind != "IMPORT" for i in items)
        recursive = sum(i.depth > 1 and i.kind != "IMPORT" for i in items)
        bundle = ContextBundle(
            profile.sha256,
            target,
            policy,
            first.source_sha256,
            tuple(items),
            edge_tuple,
            tuple(sorted(external, key=lambda i: (i.module, i.reference, i.import_source))),
            tuple(sorted(unresolved, key=lambda i: (i.reference, i.reason, i.from_item))),
            tuple(sorted(omitted, key=lambda i: (i.path, i.symbol or "", i.reason))),
            tuple(sorted(warnings)),
            files,
            direct,
            recursive,
            max(i.depth for i in items),
            "",
        )
        return replace(bundle, bundle_sha256=bundle.to_dict()["bundle_sha256"])

    @staticmethod
    def _import_base(module_name: str | None, path: str, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        if module_name is None:
            return ""
        parts = module_name.split(".")
        package = parts if path.endswith("/__init__.py") else parts[:-1]
        if node.level > len(package):
            return ""
        return ".".join(
            [*package[: len(package) - node.level + 1], *([node.module] if node.module else [])]
        )
