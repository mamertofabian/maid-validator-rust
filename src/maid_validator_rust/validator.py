"""Public Rust validator implementation."""

from __future__ import annotations

from pathlib import Path

from maid_runner.validators.base import (
    BaseValidator,
    CollectionResult,
    DependencyCollectionResult,
)

from maid_validator_rust._behavioral import _collect_behavioral
from maid_validator_rust._dependencies import _dependencies
from maid_validator_rust._implementation import _collect_implementation
from maid_validator_rust._modules import _module_path, _source_context
from maid_validator_rust._parse import _parse, _test_functions, _text


def _type_tokens(source: str) -> tuple[tuple[str, str], ...] | None:
    encoded, root, errors = _parse(f"type __MaidType = {source};")
    if errors or root is None:
        return None
    alias = next(
        (child for child in root.named_children if child.type == "type_item"), None
    )
    declared = alias.child_by_field_name("type") if alias is not None else None
    if declared is None:
        return None
    tokens: list[tuple[str, str]] = []

    def visit(node):
        if node.type in {"line_comment", "block_comment"}:
            return
        if not node.children or "literal" in node.type:
            tokens.append((node.type, _text(node, encoded)))
        else:
            for child in node.children:
                visit(child)

    visit(declared)
    return tuple(tokens)


class RustValidator(BaseValidator):
    """Collect Rust API definitions and source-visible test references."""

    @classmethod
    def supported_extensions(cls) -> tuple[str, ...]:
        return (".rs",)

    def collect_implementation_artifacts(
        self, source: str, file_path: str | Path
    ) -> CollectionResult:
        encoded, root, errors = _parse(source)
        module, _ = _source_context(file_path) if not errors else (None, {})
        artifacts = (
            []
            if errors or root is None
            else _collect_implementation(root, encoded, source_module=module)
        )
        return CollectionResult(artifacts, "rust", str(file_path), errors)

    def collect_behavioral_artifacts(
        self, source: str, file_path: str | Path
    ) -> CollectionResult:
        encoded, root, errors = _parse(source)
        module, module_map = _source_context(file_path) if not errors else (None, {})
        artifacts = (
            []
            if errors or root is None
            else _collect_behavioral(root, encoded, module, module_map)
        )
        return CollectionResult(artifacts, "rust", str(file_path), errors)

    def get_test_function_bodies(
        self, source: str, file_path: str | Path
    ) -> dict[str, str]:
        encoded, root, errors = _parse(source)
        if errors:
            raise ValueError("; ".join(errors))
        if root is None:
            return {}
        return {
            name: _text(node.child_by_field_name("body"), encoded)
            for name, node in _test_functions(root, encoded)
        }

    def types_match(
        self, manifest_type: str | None, implementation_type: str | None
    ) -> bool:
        if manifest_type is None:
            return True
        if implementation_type is None:
            return False
        declared = _type_tokens(manifest_type)
        implemented = _type_tokens(implementation_type)
        return declared is not None and declared == implemented

    def collect_dependencies(
        self, source: str, file_path: str | Path, project_root: Path
    ) -> DependencyCollectionResult:
        encoded, root, errors = _parse(source)
        if errors or root is None:
            return DependencyCollectionResult(errors=tuple(errors))
        return _dependencies(root, encoded, file_path, project_root)

    def module_path(self, file_path: str | Path, project_root: Path) -> str | None:
        return _module_path(file_path, project_root)
