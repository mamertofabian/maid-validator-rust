"""Bounded Rust test references with source-visible lexical bindings."""

from __future__ import annotations

from pathlib import Path

from maid_runner.core.types import ArtifactKind
from maid_runner.validators.base import FoundArtifact
from tree_sitter import Node

from maid_validator_rust._implementation import _collect_implementation, _type_name
from maid_validator_rust._parse import (
    _is_test,
    _items,
    _name,
    _parse,
    _test_scope_enabled,
    _text,
)


def _crate_identity(module: str) -> str:
    if module.startswith("__rust_file__."):
        return module
    parts = module.split(".")
    return (
        ".".join(parts[:3])
        if len(parts) >= 3
        and parts[1] in {"__bin__", "__test__", "__example__", "__bench__"}
        else parts[0]
    )


def _parent_path(path: str, prefix: str, module: str | None) -> str | None:
    count = 0
    while path.startswith("super::"):
        count += 1
        path = path[7:]
    if module is not None:
        parts = (
            module + ("." + prefix.rstrip(":").replace("::", ".") if prefix else "")
        ).split(".")
        if len(parts) - count < len(_crate_identity(module).split(".")):
            return None
        return "@" + ".".join(parts[:-count]) + "." + path.replace("::", ".")
    parts = prefix.rstrip(":").split("::") if prefix else []
    if count > len(parts):
        return None
    parent = "::".join(parts[:-count])
    return (parent + "::" if parent else "") + path


def _enum_variants(node: Node, source: bytes, prefix: str = "") -> dict[str, set[str]]:
    variants: dict[str, set[str]] = {}
    for child, attrs in _items(node, source):
        if not _test_scope_enabled(attrs):
            continue
        body = child.child_by_field_name("body")
        if child.type == "enum_item" and body is not None:
            variants[prefix + _name(child, source)] = {
                _name(value, source)
                for value in body.named_children
                if value.type == "enum_variant"
            }
        elif child.type == "mod_item" and body is not None:
            variants.update(
                _enum_variants(body, source, prefix + _name(child, source) + "::")
            )
    return variants


def _unit_structs(
    node: Node, source: bytes, prefix: str = "", callable_types: bool = False
) -> set[str]:
    units: set[str] = set()
    for child, attrs in _items(node, source):
        if not _test_scope_enabled(attrs):
            continue
        body = child.child_by_field_name("body")
        if child.type == "struct_item" and (
            body is None
            or (callable_types and body.type == "ordered_field_declaration_list")
        ):
            units.add(prefix + _name(child, source))
        elif child.type == "mod_item" and body is not None:
            units.update(
                _unit_structs(
                    body, source, prefix + _name(child, source) + "::", callable_types
                )
            )
    return units


def _expression_path(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "generic_function":
        return _expression_path(node.child_by_field_name("function"), source)
    if node.type == "generic_type":
        return _expression_path(node.child_by_field_name("type"), source)
    if node.type == "scoped_identifier":
        return (
            _expression_path(node.child_by_field_name("path"), source)
            + "::"
            + _text(node.child_by_field_name("name"), source)
        )
    return _text(node, source)


def _imports(
    node: Node,
    source: bytes,
    prefix: str = "",
    local_modules: set[str] | None = None,
    origins: dict[str, str] | None = None,
    source_module: str | None = None,
    visible_bindings: dict[str, str | None] | None = None,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    raw_bindings: dict[str, str] = {}
    local_modules = set(local_modules or ()) | {
        _name(child, source)
        for child, _ in _items(node, source)
        if child.type == "mod_item"
    }
    visible_bindings = visible_bindings or {}

    def bind(alias: str, original: str) -> None:
        raw_bindings[alias] = original

    def visit(item: Node, path: str = "") -> None:
        if item.type == "scoped_use_list":
            qualifier = _text(item.child_by_field_name("path"), source)
            listing = item.child_by_field_name("list")
            if listing:
                for child in listing.named_children:
                    visit(child, path + qualifier + "::")
        elif item.type == "use_list":
            for child in item.named_children:
                visit(child, path)
        elif item.type == "use_as_clause":
            original = path + _text(item.child_by_field_name("path"), source)
            bind(_text(item.child_by_field_name("alias"), source), original)
        elif item.type != "use_wildcard":
            original = path + _text(item, source)
            alias = original.rsplit("::", 1)[-1]
            if alias == "self" and "::" in original:
                alias = original.rsplit("::", 1)[0].rsplit("::", 1)[-1]
            bind(alias, original)

    def canonical(path: str, seen: frozenset[str] = frozenset()) -> str | None:
        if path.startswith("::"):
            return "@" + path[2:].replace("::", ".")
        path = path.removesuffix("::self")
        if path.startswith("super::"):
            return _parent_path(path, prefix, source_module)
        if path.startswith("self::"):
            if source_module is not None:
                return (
                    "@" + source_module + "." + (prefix + path[6:]).replace("::", ".")
                )
            return prefix + path[6:]
        if path.startswith("crate::"):
            if source_module is not None:
                return (
                    "@"
                    + _crate_identity(source_module)
                    + "."
                    + path[7:].replace("::", ".")
                )
            return path[7:]
        first, separator, rest = path.partition("::")
        if first in raw_bindings:
            if first in seen:
                return None
            bound = canonical(raw_bindings[first], seen | {first})
            return (
                None
                if bound is None
                else bound + (separator + rest if separator else "")
            )
        if first in visible_bindings:
            bound = visible_bindings[first]
            return (
                None
                if bound is None
                else bound + (separator + rest if separator else "")
            )
        if path.split("::", 1)[0] in local_modules:
            if source_module is not None:
                return "@" + source_module + "." + (prefix + path).replace("::", ".")
            return prefix + path
        if source_module is not None:
            return "@" + path.replace("::", ".")
        if path == "std" or path.startswith("std::"):
            return "@" + path.replace("::", ".")
        # Cargo crate roots are not part of the per-file API artifact name.
        return path.split("::", 1)[-1]

    for child, attrs in _items(node, source):
        if child.type == "extern_crate_declaration" and _test_scope_enabled(attrs):
            original = _text(child.child_by_field_name("name"), source)
            alias = _text(child.child_by_field_name("alias"), source) or original
            crate = (
                _crate_identity(source_module)
                if original == "self" and source_module is not None
                else original
            )
            bind(alias, "::" + crate)
        if child.type == "use_declaration" and _test_scope_enabled(attrs):
            argument = child.child_by_field_name("argument")
            if argument:
                visit(argument)
    for alias, original in raw_bindings.items():
        identity = canonical(original, frozenset({alias}))
        if identity is not None:
            bindings[alias] = identity
    return bindings


def _collect_behavioral(
    root: Node,
    source: bytes,
    source_module: str | None = None,
    module_map: dict[str, Path] | None = None,
) -> list[FoundArtifact]:
    artifacts: list[FoundArtifact] = []
    known_types: dict[str, ArtifactKind] = {}
    shadowed_macros: set[str] = set()
    namespace_symbols: dict[str, str | None] = {}
    origins: dict[str, str] = {}
    local_modules = {
        _name(node, source)
        for node, _ in _items(root, source)
        if node.type == "mod_item"
    }
    module_map = module_map or {}
    type_catalog: dict[str, ArtifactKind] = {}
    method_locations: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for definition in _collect_implementation(root, source):
        if definition.kind in {
            ArtifactKind.CLASS,
            ArtifactKind.ENUM,
            ArtifactKind.INTERFACE,
            ArtifactKind.TYPE,
        }:
            type_catalog[
                (source_module + "." if source_module else "")
                + definition.name.replace("::", ".")
            ] = definition.kind
    enum_variants = {
        name.replace("::", "."): values
        for name, values in _enum_variants(root, source).items()
    }
    unit_structs = {name.replace("::", ".") for name in _unit_structs(root, source)}
    callable_structs = {
        name.replace("::", ".")
        for name in _unit_structs(root, source, callable_types=True)
    }
    for module, path in module_map.items():
        if module == source_module:
            encoded, parsed = source, root
        else:
            encoded, parsed, errors = _parse(path.read_text(encoding="utf-8"))
            if errors or parsed is None:
                continue
        for definition in _collect_implementation(
            parsed, encoded, source_module=module
        ):
            owner_identity = getattr(definition, "_owner_identity", None)
            if (
                definition.kind == ArtifactKind.METHOD
                and owner_identity
                and definition.of
            ):
                method_locations.setdefault(
                    (owner_identity, definition.name), []
                ).append((module, definition.of))
            if definition.kind in {
                ArtifactKind.CLASS,
                ArtifactKind.ENUM,
                ArtifactKind.INTERFACE,
                ArtifactKind.TYPE,
            }:
                type_catalog[module + "." + definition.name.replace("::", ".")] = (
                    definition.kind
                )

        for name, values in _enum_variants(parsed, encoded).items():
            enum_variants[module + "." + name.replace("::", ".")] = values
        unit_structs.update(
            module + "." + name.replace("::", ".")
            for name in _unit_structs(parsed, encoded)
        )

        callable_structs.update(
            module + "." + name.replace("::", ".")
            for name in _unit_structs(parsed, encoded, callable_types=True)
        )

    def full_identity(identity: str) -> str:
        if identity.startswith("@"):
            return identity[1:].replace("::", ".")
        full = (source_module + "::" if source_module else "") + identity
        for candidate in sorted(origins, key=len, reverse=True):
            if identity == candidate or identity.startswith(candidate + "::"):
                full = origins[candidate] + identity[len(candidate) :]
                break
        return full.replace("::", ".")

    def variant_owner(identity: str) -> str | None:
        qualified = full_identity(identity)
        parent, _, variant = qualified.rpartition(".")
        if variant in enum_variants.get(parent, set()):
            return (
                "@" + parent
                if source_module is not None or identity.startswith("@")
                else parent.replace(".", "::")
            )
        return None

    def location(identity: str) -> tuple[str, str | None]:
        if source_module is None:
            if identity.startswith("@"):
                module, _, name = identity[1:].replace("::", ".").rpartition(".")
                return name, module or None
            return identity, None
        dotted = full_identity(identity)
        for module in sorted(module_map, key=len, reverse=True):
            if dotted.startswith(module + "."):
                return dotted[len(module) + 1 :].replace(".", "::"), module
        module, _, name = dotted.rpartition(".")
        return name, module or None

    def emit(kind: ArtifactKind, name: str, owner: str | None = None) -> None:
        if name:
            qualified_owner = full_identity(owner) if owner is not None else None
            if owner is not None:
                owner, module = location(owner)
            else:
                name, module = location(name)
            artifacts.append(
                FoundArtifact(
                    kind=kind,
                    name=name,
                    of=owner,
                    import_source=module,
                    reference_context="exact",
                )
            )
            if kind == ArtifactKind.METHOD and qualified_owner is not None:
                for implementation_module, implementation_owner in method_locations.get(
                    (qualified_owner, name), ()
                ):
                    artifacts.append(
                        FoundArtifact(
                            kind=kind,
                            name=name,
                            of=implementation_owner,
                            import_source=implementation_module,
                            reference_context="exact",
                        )
                    )

    def resolve(
        path: str, symbols: dict[str, str | None], prefix: str, namespace: bool = False
    ) -> str | None:
        if path.startswith("::"):
            return "@" + path[2:].replace("::", ".")
        if path.startswith("super::"):
            return _parent_path(path, prefix, source_module)
        if path.startswith("self::"):
            return prefix + path[6:]
        if path.startswith("crate::"):
            if source_module is not None:
                return (
                    "@"
                    + _crate_identity(source_module)
                    + "."
                    + path[7:].replace("::", ".")
                )
            return path[7:]
        first, separator, rest = path.partition("::")
        if namespace or separator:
            symbols = namespace_symbols
        if first in symbols:
            bound = symbols[first]
            return (
                None
                if bound is None
                else bound + (separator + rest if separator else "")
            )
        if source_module is not None and first in module_map:
            return "@" + path.replace("::", ".")
        return None

    def receiver_type(
        node: Node | None,
        values: dict[str, str | None],
        symbols: dict[str, str | None],
        prefix: str,
        encoded: bytes,
    ) -> str | None:
        if node is None:
            return None
        if node.type == "identifier":
            return values.get(_text(node, encoded))
        if (
            node.type in {"reference_expression", "parenthesized_expression"}
            and node.named_children
        ):
            return receiver_type(
                node.named_children[-1], values, symbols, prefix, encoded
            )
        if node.type == "struct_expression":
            return resolve(
                _text(node.child_by_field_name("name"), encoded), symbols, prefix, True
            )
        return None

    def visit(
        node: Node,
        encoded: bytes,
        values: dict[str, str | None],
        symbols: dict[str, str | None],
        prefix: str,
    ) -> None:
        if node.type == "macro_definition":
            shadowed_macros.add(_name(node, encoded))
            return
        if node.type in {
            "closure_expression",
            "async_block",
            "function_item",
            "mod_item",
            "line_comment",
            "block_comment",
        }:
            return
        if node.type == "block":
            inherited_origins = dict(origins)
            inherited_types = dict(known_types)
            inherited_macros = set(shadowed_macros)
            inherited_namespaces = dict(namespace_symbols)
            local_values = dict(values)
            local_symbols = dict(symbols)
            local_symbols.update(
                _imports(
                    node,
                    encoded,
                    prefix,
                    local_modules,
                    origins,
                    source_module,
                    symbols,
                )
            )
            for alias in _imports(
                node, encoded, prefix, local_modules, origins, source_module, symbols
            ):
                if (
                    alias in {"assert", "assert_eq", "assert_ne"}
                    and local_symbols[alias] != "@std." + alias
                ):
                    shadowed_macros.add(alias)
                if alias == "std" and local_symbols[alias] != "@std":
                    shadowed_macros.add("std::*")
            namespace_symbols.update(
                _imports(
                    node,
                    encoded,
                    prefix,
                    local_modules,
                    origins,
                    source_module,
                    {**symbols, **namespace_symbols},
                )
            )
            for child, attrs in _items(node, encoded):
                if not _test_scope_enabled(attrs):
                    continue
                if child.type == "mod_item" and _name(child, encoded) == "std":
                    shadowed_macros.add("std::*")
                if child.type in {"function_item", "const_item", "static_item"}:
                    local_symbols[_name(child, encoded)] = None
                if child.type in {
                    "struct_item",
                    "enum_item",
                    "type_item",
                    "trait_item",
                    "mod_item",
                }:
                    namespace_symbols[_name(child, encoded)] = None
                    body = child.child_by_field_name("body")
                    if child.type == "struct_item" and (
                        body is None or body.type == "ordered_field_declaration_list"
                    ):
                        local_symbols[_name(child, encoded)] = None
            for child, attrs in _items(node, encoded):
                if _test_scope_enabled(attrs):
                    visit(child, encoded, local_values, local_symbols, prefix)
            origins.clear()
            origins.update(inherited_origins)
            known_types.clear()
            known_types.update(inherited_types)
            shadowed_macros.clear()
            shadowed_macros.update(inherited_macros)
            namespace_symbols.clear()
            namespace_symbols.update(inherited_namespaces)
            return
        if node.type in {"for_expression", "match_arm", "let_condition"}:
            bound_values = dict(values)
            bound_symbols = dict(symbols)
            pattern = node.child_by_field_name("pattern")
            iterator = (
                node.child_by_field_name("value")
                if node.type == "for_expression"
                else None
            )
            if iterator is not None:
                visit(iterator, encoded, values, symbols, prefix)
            if pattern is not None:
                shadow(pattern, bound_values, bound_symbols, encoded)
            for child in node.named_children:
                if child != pattern and child != iterator:
                    visit(child, encoded, bound_values, bound_symbols, prefix)
            return
        if node.type in {"if_expression", "while_expression"}:
            condition = node.child_by_field_name("condition")
            bound_values = dict(values)
            bound_symbols = dict(symbols)
            if condition is not None and condition.type in {
                "let_condition",
                "let_chain",
            }:
                clauses = (
                    condition.named_children
                    if condition.type == "let_chain"
                    else [condition]
                )
                for clause in clauses:
                    if clause.type == "let_condition":
                        value = clause.child_by_field_name("value")
                        if value is not None:
                            visit(value, encoded, bound_values, bound_symbols, prefix)
                        pattern = clause.child_by_field_name("pattern")
                        if pattern is not None:
                            shadow(pattern, bound_values, bound_symbols, encoded)
                    else:
                        visit(clause, encoded, bound_values, bound_symbols, prefix)
                for child in node.named_children:
                    if child != condition:
                        if child == node.child_by_field_name("alternative"):
                            visit(child, encoded, values, symbols, prefix)
                        else:
                            visit(child, encoded, bound_values, bound_symbols, prefix)
                return
        if node.type == "let_declaration":
            annotation = node.child_by_field_name("type")
            declared = _type_name(annotation, encoded) if annotation else ""
            identity = resolve(declared, symbols, prefix, True) if declared else None
            if identity:
                kind = type_catalog.get(
                    full_identity(identity),
                    known_types.get(identity, ArtifactKind.CLASS),
                )
                known_types[identity] = kind
                if kind in {ArtifactKind.TYPE, ArtifactKind.INTERFACE}:
                    emit(kind, identity)
            value = node.child_by_field_name("value")
            if value:
                visit(value, encoded, values, symbols, prefix)
            pattern = node.child_by_field_name("pattern")
            if pattern is not None:
                while (
                    pattern.type in {"ref_pattern", "mut_pattern"}
                    and pattern.named_children
                ):
                    pattern = pattern.named_children[-1]
                if pattern.type == "identifier":
                    name = _text(pattern, encoded)
                    annotation = node.child_by_field_name("type")
                    declared = (
                        _type_name(annotation, encoded).lstrip("&").removeprefix("mut ")
                        if annotation
                        else ""
                    )
                    values[name] = (
                        resolve(declared, symbols, prefix, True)
                        if declared
                        else receiver_type(value, values, symbols, prefix, encoded)
                    )
                    symbols[name] = None
                else:
                    # Unsupported destructuring cannot preserve older bindings.
                    shadow(pattern, values, symbols, encoded)
            return
        if node.type == "macro_invocation":
            macro = _text(node.child_by_field_name("macro"), encoded)
            if macro in shadowed_macros or (
                macro.startswith("std::") and "std::*" in shadowed_macros
            ):
                return
            if macro not in {
                "assert",
                "assert_eq",
                "assert_ne",
                "std::assert",
                "std::assert_eq",
                "std::assert_ne",
            }:
                return
            tokens = next(
                (c for c in node.named_children if c.type == "token_tree"), None
            )
            if tokens is None:
                return
            # Parse assertion arguments as Rust expressions, retaining tokens,
            # comments and strings rather than scanning text for call names.
            arguments = _text(tokens, encoded)[1:-1]
            fragment, parsed, errors = _parse(
                f"fn __assertion() {{ __args({arguments}); }}"
            )
            if parsed is not None and not errors:
                function = parsed.named_children[0]
                body = function.child_by_field_name("body")
                if body:
                    for statement in body.named_children:
                        call = (
                            statement.named_children[0]
                            if statement.type == "expression_statement"
                            else statement
                        )
                        args = call.child_by_field_name("arguments")
                        if args:
                            eager = 1 if macro.endswith("assert") else 2
                            for argument in args.named_children[:eager]:
                                visit(argument, fragment, values, symbols, prefix)
            return
        if node.type == "identifier":
            identity = resolve(_text(node, encoded), symbols, prefix)
            if identity is not None and full_identity(identity) in unit_structs:
                emit(ArtifactKind.CLASS, identity)
            if identity is not None and variant_owner(identity) is not None:
                emit(ArtifactKind.ENUM, variant_owner(identity))
        if node.type == "struct_expression":
            path = _text(node.child_by_field_name("name"), encoded)
            owner = resolve(path, symbols, prefix, True)
            qualifier = resolve(path.rpartition("::")[0], symbols, prefix, True)
            if (
                qualifier
                and (
                    known_types.get(qualifier)
                    or type_catalog.get(full_identity(qualifier))
                )
                == ArtifactKind.ENUM
            ):
                emit(ArtifactKind.ENUM, qualifier)
                owner = None
            if owner:
                emit(
                    known_types.get(
                        owner,
                        type_catalog.get(full_identity(owner), ArtifactKind.CLASS),
                    ),
                    owner,
                )
        if node.type == "scoped_identifier":
            identity = resolve(_expression_path(node, encoded), symbols, prefix, True)
            if identity is not None and full_identity(identity) in unit_structs:
                emit(ArtifactKind.CLASS, identity)
            qualifier = resolve(
                _expression_path(node, encoded).rpartition("::")[0],
                symbols,
                prefix,
                True,
            )
            if (
                qualifier
                and (
                    known_types.get(qualifier)
                    or type_catalog.get(full_identity(qualifier))
                )
                == ArtifactKind.ENUM
            ):
                emit(ArtifactKind.ENUM, qualifier)
        if node.type == "field_expression":
            owner = receiver_type(
                node.child_by_field_name("value"), values, symbols, prefix, encoded
            )
            if owner:
                emit(
                    ArtifactKind.ATTRIBUTE,
                    _text(node.child_by_field_name("field"), encoded),
                    owner,
                )
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            parenthesized = False
            while (
                function is not None
                and function.type == "parenthesized_expression"
                and function.named_children
            ):
                parenthesized = True
                function = function.named_children[0]
            if function is not None and function.type == "generic_function":
                function = function.child_by_field_name("function")
            if function:
                if function.type == "field_expression":
                    owner = receiver_type(
                        function.child_by_field_name("value"),
                        values,
                        symbols,
                        prefix,
                        encoded,
                    )
                    if owner:
                        emit(
                            (
                                ArtifactKind.ATTRIBUTE
                                if parenthesized
                                else ArtifactKind.METHOD
                            ),
                            _text(function.child_by_field_name("field"), encoded),
                            owner,
                        )
                    # A method name must not also become field-access coverage.
                    value = function.child_by_field_name("value")
                    if value:
                        visit(value, encoded, values, symbols, prefix)
                else:
                    path = _expression_path(function, encoded)
                    if "::" in path:
                        qualifier, _, method = path.rpartition("::")
                        owner = resolve(qualifier, symbols, prefix, True)
                        owner_kind = known_types.get(owner) if owner else None
                        if owner and owner_kind is None:
                            owner_kind = type_catalog.get(full_identity(owner))
                        if owner and owner_kind is not None:
                            emit(owner_kind, owner)
                            if not (
                                owner_kind == ArtifactKind.ENUM
                                and method
                                in enum_variants.get(full_identity(owner), set())
                            ):
                                emit(ArtifactKind.METHOD, method, owner)
                        else:
                            name = resolve(path, symbols, prefix)
                            if name:
                                emit(
                                    (
                                        ArtifactKind.CLASS
                                        if full_identity(name) in callable_structs
                                        else ArtifactKind.FUNCTION
                                    ),
                                    name,
                                )
                    else:
                        name = resolve(path, symbols, prefix)
                        if name:
                            enum = variant_owner(name)
                            if enum is not None:
                                emit(ArtifactKind.ENUM, enum)
                            else:
                                emit(
                                    (
                                        ArtifactKind.CLASS
                                        if full_identity(name) in callable_structs
                                        else ArtifactKind.FUNCTION
                                    ),
                                    name,
                                )
                args = node.child_by_field_name("arguments")
                if args:
                    for argument in args.named_children:
                        visit(argument, encoded, values, symbols, prefix)
            return
        for child in node.named_children:
            visit(child, encoded, values, symbols, prefix)

    def shadow(
        pattern: Node,
        values: dict[str, str | None],
        symbols: dict[str, str | None],
        encoded: bytes,
    ) -> None:
        if pattern.type in {"identifier", "shorthand_field_identifier"}:
            name = _text(pattern, encoded)
            values[name] = None
            symbols[name] = None
        else:
            pattern_type = pattern.child_by_field_name("type")
            for child in pattern.named_children:
                if child != pattern_type:
                    shadow(child, values, symbols, encoded)

    def scope(node: Node, prefix: str, inherited: dict[str, str | None]) -> None:
        inherited_origins = dict(origins)
        inherited_types = dict(known_types)
        inherited_macros = set(shadowed_macros)
        inherited_namespaces = dict(namespace_symbols)
        symbols: dict[str, str | None] = {}
        if not prefix or any(
            child.type == "use_declaration"
            and _text(child.child_by_field_name("argument"), source) == "super::*"
            for child, _ in _items(node, source)
        ):
            symbols.update(inherited)
        if prefix and not symbols:
            namespace_symbols.clear()
        for child, attrs in _items(node, source):
            if not _test_scope_enabled(attrs):
                continue
            if child.type in {"const_item", "static_item"}:
                symbols[_name(child, source)] = None
            if child.type == "mod_item" and _name(child, source) == "std":
                shadowed_macros.add("std::*")
            if child.type in {
                "function_item",
                "struct_item",
                "enum_item",
                "trait_item",
                "type_item",
                "mod_item",
            }:
                identity = prefix + _name(child, source)
                if child.type == "function_item":
                    symbols[_name(child, source)] = identity
                elif child.type == "struct_item":
                    body = child.child_by_field_name("body")
                    if body is None or body.type == "ordered_field_declaration_list":
                        symbols[_name(child, source)] = identity
                    namespace_symbols[_name(child, source)] = identity
                else:
                    namespace_symbols[_name(child, source)] = identity
                if child.type not in {"function_item", "mod_item"}:
                    known_types[prefix + _name(child, source)] = {
                        "struct_item": ArtifactKind.CLASS,
                        "enum_item": ArtifactKind.ENUM,
                        "trait_item": ArtifactKind.INTERFACE,
                        "type_item": ArtifactKind.TYPE,
                    }[child.type]
        namespace_symbols.update(
            _imports(
                node,
                source,
                prefix,
                local_modules,
                origins,
                source_module,
                {**symbols, **namespace_symbols},
            )
        )
        symbols.update(
            _imports(
                node, source, prefix, local_modules, origins, source_module, symbols
            )
        )
        for alias in _imports(
            node, source, prefix, local_modules, origins, source_module, inherited
        ):
            if (
                alias in {"assert", "assert_eq", "assert_ne"}
                and symbols[alias] != "@std." + alias
            ):
                shadowed_macros.add(alias)
            if alias == "std" and symbols[alias] != "@std":
                shadowed_macros.add("std::*")
        for child, attrs in _items(node, source):
            if child.type == "macro_definition" and _test_scope_enabled(attrs):
                shadowed_macros.add(_name(child, source))
            if child.type == "function_item" and _is_test(attrs):

                body = child.child_by_field_name("body")
                emit(ArtifactKind.TEST_FUNCTION, prefix + _name(child, source))
                if body:
                    visit(body, source, {}, symbols, prefix)
            elif child.type == "mod_item":
                body = child.child_by_field_name("body")
                if body and _test_scope_enabled(attrs):
                    scope(body, prefix + _name(child, source) + "::", symbols)
        origins.clear()
        origins.update(inherited_origins)
        known_types.clear()
        known_types.update(inherited_types)
        shadowed_macros.clear()
        shadowed_macros.update(inherited_macros)
        namespace_symbols.clear()
        namespace_symbols.update(inherited_namespaces)

    scope(root, "", {})
    # Preserve deterministic first-seen order while deduplicating references.
    return list(dict.fromkeys(artifacts))
