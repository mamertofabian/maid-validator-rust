"""Rust public API definitions, without compiler-dependent expansion."""

from __future__ import annotations

from dataclasses import dataclass

from maid_runner.core.types import ArgSpec, ArtifactKind
from maid_runner.validators.base import FoundArtifact
from tree_sitter import Node

from maid_validator_rust._parse import (
    _items,
    _name,
    _public,
    _test_scope_enabled,
    _text,
)


@dataclass(frozen=True)
class _RustArtifact(FoundArtifact):
    _owner_identity: str | None = None

    @property
    def is_private(self) -> bool:
        return any(
            part.startswith("_")
            for identity in (self.name, self.of or "")
            for part in identity.split("::")
        )


def _type_name(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "generic_type":
        return _type_name(node.child_by_field_name("type"), source)
    if node.type == "reference_type":
        return _type_name(node.child_by_field_name("type"), source)
    return _text(node, source)


def _owner_name(name: str, prefix: str) -> str:
    if name.startswith("crate::"):
        return name[7:]
    if name.startswith("self::"):
        return prefix + name[6:]
    parts = prefix.rstrip(":").split("::") if prefix else []
    while name.startswith("super::"):
        name = name[7:]
        parts = parts[:-1]
    return ("::".join(parts) + "::" if parts else "") + name


def _production_attributes(attrs: tuple[str, ...]) -> bool:
    return _test_scope_enabled(attrs) and not any(
        "".join(a.split()) in {"#[test]", "#[cfg(test)]"} for a in attrs
    )


def _generics(node: Node, source: bytes) -> tuple[str, ...]:
    parameters = node.child_by_field_name("type_parameters")
    if parameters is None:
        return ()
    return tuple(
        _text(child.child_by_field_name("name"), source)
        for child in parameters.named_children
        if child.child_by_field_name("name") is not None
    )


def _function(
    node: Node,
    source: bytes,
    name: str,
    owner: str | None = None,
    owner_identity: str | None = None,
) -> FoundArtifact:
    parameters = node.child_by_field_name("parameters")
    args = []
    if parameters is not None:
        for parameter in parameters.named_children:
            if parameter.type == "self_parameter":
                continue
            pattern = parameter.child_by_field_name("pattern")
            if _text(pattern, source) == "self":
                continue
            annotation = parameter.child_by_field_name("type")
            args.append(
                ArgSpec(
                    name=_text(pattern, source), type=_text(annotation, source) or None
                )
            )
    modifiers = next(
        (c for c in node.named_children if c.type == "function_modifiers"), None
    )
    return _RustArtifact(
        kind=ArtifactKind.METHOD if owner else ArtifactKind.FUNCTION,
        name=name,
        of=owner,
        args=tuple(args),
        returns=_text(node.child_by_field_name("return_type"), source) or "()",
        type_parameters=_generics(node, source),
        is_async="async" in _text(modifiers, source).split(),
        line=node.start_point.row + 1,
        _owner_identity=owner_identity,
    )


def _private_visibility(
    root: Node, source: bytes, prefix: str = "", public_parent: bool = True
) -> tuple[set[str], set[str]]:
    types: set[str] = set()
    traits: set[str] = set()
    for child, attrs in _items(root, source):
        if not _production_attributes(attrs):
            continue
        name = prefix + _name(child, source)
        public = public_parent and _public(child, source)
        body = child.child_by_field_name("body")
        if child.type == "mod_item" and body is not None:
            nested_types, nested_traits = _private_visibility(
                body, source, name + "::", public
            )
            types.update(nested_types)
            traits.update(nested_traits)
        elif child.type in {"struct_item", "enum_item", "type_item"} and not public:
            types.add(name)
        elif child.type == "trait_item" and not public:
            traits.add(name)
    return types, traits


def _implementation_aliases(
    root: Node, source: bytes, prefix: str, source_module: str | None = None
) -> dict[str, str]:
    from maid_validator_rust._behavioral import _imports

    local_names = {
        _name(node, source)
        for node, _ in _items(root, source)
        if node.type
        in {"struct_item", "enum_item", "trait_item", "type_item", "mod_item"}
    }
    aliases = _imports(
        root,
        source,
        prefix,
        local_names,
        source_module=source_module or "__maid_impl__",
    )
    if source_module is not None:
        return aliases
    return {
        name: identity.removeprefix("@__maid_impl__.")
        .removeprefix("@")
        .replace(".", "::")
        for name, identity in aliases.items()
    }


def _aliased_owner(name: str, prefix: str, aliases: dict[str, str]) -> str:
    first, separator, rest = name.partition("::")
    if first in aliases:
        return aliases[first] + (separator + rest if separator else "")
    return _owner_name(name, prefix)


def _project_owner(
    name: str, prefix: str, aliases: dict[str, str], module: str
) -> tuple[str, str]:
    from maid_validator_rust._behavioral import _crate_identity, _parent_path

    first, separator, rest = name.partition("::")
    if first in aliases:
        identity = aliases[first] + (separator + rest if separator else "")
    elif name.startswith("crate::"):
        identity = "@" + _crate_identity(module) + "." + name[7:]
    elif name.startswith("super::"):
        identity = _parent_path(name, prefix, module) or ""
    else:
        identity = "@" + module + "." + _owner_name(name, prefix)
    identity = identity.removeprefix("@").replace("::", ".")
    if identity.startswith(module + "."):
        owner = identity[len(module) + 1 :].replace(".", "::")
    elif identity.startswith(_crate_identity(module) + "."):
        owner = "crate::" + identity[len(_crate_identity(module)) + 1 :].replace(
            ".", "::"
        )
    else:
        owner = "::" + identity.replace(".", "::")
    return owner, identity


def _collect_implementation(
    root: Node,
    source: bytes,
    prefix: str = "",
    visibility: tuple[set[str], set[str]] | None = None,
    source_module: str | None = None,
) -> list[FoundArtifact]:
    artifacts = []
    visibility = visibility or _private_visibility(root, source, prefix)
    private_types, private_traits = visibility
    aliases = _implementation_aliases(root, source, prefix)
    project_aliases = (
        _implementation_aliases(root, source, prefix, source_module)
        if source_module is not None
        else {}
    )
    for node, attrs in _items(root, source):
        if not _production_attributes(attrs):
            continue
        if any("".join(a.split()) == "#[cfg(test)]" for a in attrs):
            continue
        name = _name(node, source)
        visible = _public(node, source) or name.startswith("_")
        full_name = prefix + name
        if node.type == "mod_item":
            body = node.child_by_field_name("body")
            if body is not None and _public(node, source):
                artifacts.extend(
                    _collect_implementation(
                        body, source, full_name + "::", visibility, source_module
                    )
                )
        elif (
            node.type in {"struct_item", "trait_item", "enum_item", "type_item"}
            and visible
        ):
            kind = {
                "struct_item": ArtifactKind.CLASS,
                "trait_item": ArtifactKind.INTERFACE,
                "enum_item": ArtifactKind.ENUM,
                "type_item": ArtifactKind.TYPE,
            }[node.type]
            artifacts.append(
                _RustArtifact(
                    kind=kind,
                    name=full_name,
                    type_parameters=_generics(node, source),
                    type_annotation=_text(node.child_by_field_name("type"), source)
                    or None,
                    line=node.start_point.row + 1,
                )
            )
            body = node.child_by_field_name("body")
            if body is not None:
                for child, attributes in _items(body, source):
                    if not _production_attributes(attributes):
                        continue
                    if child.type == "field_declaration" and _public(child, source):
                        artifacts.append(
                            _RustArtifact(
                                kind=ArtifactKind.ATTRIBUTE,
                                name=_name(child, source),
                                of=full_name,
                                type_annotation=_text(
                                    child.child_by_field_name("type"), source
                                ),
                            )
                        )
                    elif node.type == "trait_item" and child.type in {
                        "function_item",
                        "function_signature_item",
                    }:
                        artifacts.append(
                            _function(child, source, _name(child, source), full_name)
                        )
        elif node.type == "function_item" and visible:
            artifacts.append(_function(node, source, full_name))
        elif node.type == "impl_item":
            type_name = _type_name(node.child_by_field_name("type"), source)
            owner = _aliased_owner(type_name, prefix, aliases)
            if owner in private_types:
                continue
            body = node.child_by_field_name("body")
            trait = node.child_by_field_name("trait")
            if (
                trait is not None
                and _aliased_owner(_type_name(trait, source), prefix, aliases)
                in private_traits
            ):
                continue
            if body is not None:
                owner_identity = None
                if source_module is not None:
                    owner, owner_identity = _project_owner(
                        type_name, prefix, project_aliases, source_module
                    )
                for child, attributes in _items(body, source):
                    if not _production_attributes(attributes):
                        continue
                    if child.type == "function_item" and (
                        _public(child, source)
                        or trait is not None
                        or _name(child, source).startswith("_")
                    ):
                        artifacts.append(
                            _function(
                                child,
                                source,
                                _name(child, source),
                                owner,
                                owner_identity,
                            )
                        )
    return artifacts
