"""Resolve source-local Rust mod declarations; uncertainty remains explicit."""

from __future__ import annotations

from pathlib import Path

from maid_runner.validators.base import DependencyCollectionResult
from tree_sitter import Node

from maid_validator_rust._modules import _is_crate_root
from maid_validator_rust._parse import _items, _name, _parse, _test_scope_enabled, _text


def _path_literal(literal: str) -> str:
    """Decode a grammar-checked Rust string, including raw and Unicode forms."""
    encoded, root, errors = _parse("const MAID_PATH: &str = " + literal + ";")
    if errors or root is None or len(root.named_children) != 1:
        raise ValueError("Invalid Rust path literal")
    value = root.named_children[0].child_by_field_name("value")
    if value is None or value.type not in {"string_literal", "raw_string_literal"}:
        raise ValueError("Path must be a Rust string literal")
    output: list[str] = []
    continuation = False
    escapes = {
        "\\n": "\n",
        "\\r": "\r",
        "\\t": "\t",
        "\\0": "\0",
        '\\"': '"',
        "\\'": "'",
        "\\\\": "\\",
    }
    for child in value.named_children:
        text = _text(child, encoded)
        if child.type == "string_content":
            if continuation:
                text = text.lstrip(" \t\r\n")
            output.append(text)
            continuation = continuation and not text
        elif child.type == "escape_sequence":
            continuation = False
            if text in escapes:
                output.append(escapes[text])
            elif text.startswith("\\x") and len(text) == 4:
                codepoint = int(text.replace("\\x", "0x", 1), 0)
                if codepoint > 127:
                    raise ValueError("Non-ASCII byte escape")
                output.append(chr(codepoint))
            elif text.startswith("\\u{") and text.endswith("}"):
                digits = text[3:-1].replace("_", "")
                if not 1 <= len(digits) <= 6:
                    raise ValueError("Invalid Unicode escape")
                codepoint = int(digits, 16)
                if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    raise ValueError("Invalid Unicode scalar")
                output.append(chr(codepoint))
            elif text.startswith(("\\\n", "\\\r\n")):
                continuation = True
            else:
                raise ValueError("Unsupported Rust escape")
        else:
            raise ValueError("Unsupported path literal content")
    return "".join(output)


def _dependency_edges(
    root: Node, source: bytes, file_path: str | Path, project_root: Path
) -> tuple[list[tuple[str, str]], list[str]]:
    path = (project_root / file_path).resolve()
    base = (
        path.parent
        if path.stem in {"lib", "main", "mod"} or _is_crate_root(path, project_root)
        else path.parent / path.stem
    )
    modules: list[tuple[str, str]] = []
    unresolved: list[str] = []

    def visit(
        node: Node, directory: Path, attribute_directory: Path, prefix: str = ""
    ) -> None:
        inner = tuple(
            _text(child, source).replace("#![", "#[", 1)
            for child in node.named_children
            if child.type == "inner_attribute_item"
        )
        if not _test_scope_enabled(inner):
            unresolved.append("Conditional module scope requires cfg resolution")
            return
        for child, attrs in _items(node, source):
            if child.type == "macro_invocation":
                unresolved.append(
                    "Module-level macro expansion requires compiler evidence"
                )
            if child.type != "mod_item":
                continue
            name = _name(child, source)
            compact = ["".join(a.split()) for a in attrs]
            if any(
                a.startswith(("#[cfg(", "#[cfg_attr(")) and a != "#[cfg(test)]"
                for a in compact
            ):
                unresolved.append(f"Conditional module {name} requires cfg resolution")
                continue
            body = child.child_by_field_name("body")
            if body is not None:
                nested = directory / name
                overrides = [
                    a for a in attrs if "".join(a.split()).startswith("#[path=")
                ]
                if overrides:
                    try:
                        literal = (
                            overrides[0].split("=", 1)[1].rsplit("]", 1)[0].strip()
                        )
                        nested = attribute_directory / _path_literal(literal)
                        if not nested.resolve().is_relative_to(project_root.resolve()):
                            raise ValueError("outside project")
                    except (ValueError, SyntaxError, TypeError):
                        unresolved.append(f"Unresolved inline path for module {name}")
                        continue
                visit(body, nested, nested, prefix + name + "::")
                continue
            overrides = [a for a in attrs if "".join(a.split()).startswith("#[path=")]
            if overrides:
                try:
                    literal = overrides[0].split("=", 1)[1].rsplit("]", 1)[0].strip()
                    candidate = attribute_directory / _path_literal(literal)
                    candidates = [candidate] if candidate.is_file() else []
                except (ValueError, SyntaxError, TypeError):
                    candidates = []
            else:
                candidates = [
                    p
                    for p in (directory / f"{name}.rs", directory / name / "mod.rs")
                    if p.is_file()
                ]
            if len(candidates) != 1:
                unresolved.append(f"Missing or ambiguous module {name} in {directory}")
                continue
            try:
                relative = (
                    candidates[0]
                    .resolve()
                    .relative_to(project_root.resolve())
                    .as_posix()
                )
            except ValueError:
                unresolved.append(f"Module {name} escapes the project root")
                continue
            modules.append((prefix + name, relative))

    visit(root, base, path.parent)
    return modules, unresolved


def _dependencies(
    root: Node, source: bytes, file_path: str | Path, project_root: Path
) -> DependencyCollectionResult:
    modules, unresolved = _dependency_edges(root, source, file_path, project_root)
    return DependencyCollectionResult(
        modules=tuple(sorted({path for _, path in modules})),
        unresolved=tuple(unresolved),
    )
