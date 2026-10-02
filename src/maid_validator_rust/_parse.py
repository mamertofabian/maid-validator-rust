"""Offline tree-sitter syntax adapter; recovered syntax is never accepted."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

import tree_sitter_rust
from tree_sitter import Language, Node, Parser


@lru_cache(maxsize=1)
def _language() -> Language:
    return Language(tree_sitter_rust.language())


def _parse(source: str) -> tuple[bytes, Node | None, list[str]]:
    try:
        encoded = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        return b"", None, [f"Invalid UTF-8 Rust source: {exc}"]
    root = Parser(_language()).parse(encoded).root_node
    errors = []
    if root.has_error:
        for node in _walk(root):
            if node.is_error or node.is_missing:
                row, col = node.start_point
                errors.append(f"Rust syntax error at {row + 1}:{col + 1} ({node.type})")
        if not errors:
            errors.append("Rust syntax error")
    return encoded, root, errors


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node: Node | None, source: bytes) -> str:
    return (
        "" if node is None else source[node.start_byte : node.end_byte].decode("utf-8")
    )


def _name(node: Node, source: bytes) -> str:
    return _text(node.child_by_field_name("name"), source)


def _items(node: Node, source: bytes) -> Iterator[tuple[Node, tuple[str, ...]]]:
    inner = [
        _text(child, source).replace("#![", "#[", 1)
        for child in node.named_children
        if child.type == "inner_attribute_item"
    ]
    if not _test_scope_enabled(tuple(inner)):
        return
    attributes: list[str] = []
    for child in node.named_children:
        if child.type == "attribute_item":
            attributes.append(_text(child, source))
        elif child.type not in {
            "line_comment",
            "block_comment",
            "inner_attribute_item",
        }:
            yield child, tuple(inner + attributes)
            attributes = []


def _is_test(attributes: tuple[str, ...]) -> bool:
    return _test_scope_enabled(attributes) and any(
        "".join(attr.split()) == "#[test]" for attr in attributes
    )


def _test_scope_enabled(attributes: tuple[str, ...]) -> bool:
    compact = ["".join(attr.split()) for attr in attributes]
    return not any(
        attr.startswith("#[ignore")
        or (attr.startswith(("#[cfg(", "#[cfg_attr(")) and attr != "#[cfg(test)]")
        for attr in compact
    )


def _public(node: Node, source: bytes) -> bool:
    return any(
        child.type == "visibility_modifier" and _text(child, source) == "pub"
        for child in node.named_children
    )


def _test_functions(
    node: Node, source: bytes, prefix: str = ""
) -> Iterator[tuple[str, Node]]:
    for child, attributes in _items(node, source):
        if child.type == "function_item" and _is_test(attributes):
            yield prefix + _name(child, source), child
        elif child.type == "mod_item":
            body = child.child_by_field_name("body")
            if body is not None and _test_scope_enabled(attributes):
                yield from _test_functions(
                    body, source, prefix + _name(child, source) + "::"
                )
