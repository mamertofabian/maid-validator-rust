from pathlib import Path

import pytest
from maid_runner.validators.registry import ValidatorRegistry


def test_package_and_module_export_the_validator():
    from maid_validator_rust import RustValidator
    from maid_validator_rust.validator import RustValidator as ModuleValidator

    assert RustValidator is ModuleValidator
    assert RustValidator().supported_extensions() == (".rs",)
    assert ModuleValidator().supported_extensions() == (".rs",)


def _rust_validator():
    from maid_validator_rust import RustValidator

    return RustValidator()


def _definitions(source):
    result = _rust_validator().collect_implementation_artifacts(source, "src/lib.rs")
    assert result.errors == []
    return {(a.kind.value, a.qualified_name): a for a in result.artifacts}


def _references(source):
    result = _rust_validator().collect_behavioral_artifacts(source, "tests/render.rs")
    assert result.errors == []
    return {(a.kind.value, a.qualified_name) for a in result.artifacts}


def test_public_rust_definitions_and_raw_signatures():
    artifacts = _definitions("""
        pub struct Widget<T> { pub value: T, hidden: bool }
        pub trait Draw { fn draw(&self) -> String; }
        pub enum State { Ready, Waiting(u32) }
        pub type Label = &'static str;
        pub const LIMIT: usize = 4;
        pub async fn render<'a>(value: &'a str) -> Result<String, ()> { todo!() }
        impl<T> Widget<T> {
            pub fn new(value: T) -> Self { todo!() }
            pub fn read(&self) -> &T { &self.value }
            fn secret(&self) {}
        }
        impl<T> Draw for Widget<T> { fn draw(&self) -> String { todo!() } }
        fn helper() {}
        pub(crate) fn restricted() {}
    """)
    expected = {
        ("class", "Widget"),
        ("interface", "Draw"),
        ("enum", "State"),
        ("type", "Label"),
        ("attribute", "Widget.value"),
        ("function", "render"),
        ("method", "Widget.new"),
        ("method", "Widget.read"),
        ("method", "Widget.draw"),
        ("method", "Draw.draw"),
    }
    assert set(artifacts) == expected
    render = artifacts[("function", "render")]
    assert render.is_async
    assert render.args[0].type == "&'a str"
    assert render.returns == "Result<String, ()>"
    assert artifacts[("class", "Widget")].type_parameters == ("T",)
    assert artifacts[("type", "Label")].type_annotation == "&'static str"
    assert [a.name for a in artifacts[("method", "Widget.read")].args] == []


def test_nested_modules_have_distinct_definition_names():
    artifacts = _definitions(
        "pub mod left { pub fn run() {} } pub mod right { pub fn run() {} }"
    )
    assert ("function", "left::run") in artifacts
    assert ("function", "right::run") in artifacts


def test_tests_and_private_items_stay_out_of_snapshots():
    validator = _rust_validator()
    snapshot = validator.generate_snapshot(
        "pub fn render() {} fn helper() {} #[cfg(test)] mod tests { #[test] fn checks() {} }",
        "src/lib.rs",
    )
    assert snapshot == [{"kind": "function", "name": "render", "returns": "()"}]


@pytest.mark.parametrize(
    "source", ["pub fn {", "pub struct Thing {", "pub fn f() { let x = ; }"]
)
def test_parse_errors_never_return_partial_artifacts(source):
    validator = _rust_validator()
    for collector in (
        validator.collect_implementation_artifacts,
        validator.collect_behavioral_artifacts,
    ):
        result = collector(source, "bad.rs")
        assert result.errors
        assert result.artifacts == []


def test_empty_sources_and_extension_registration():
    validator = _rust_validator()
    assert validator.supported_extensions() == (".rs",)
    assert validator.collect_implementation_artifacts("", "empty.rs").artifacts == []
    assert validator.collect_behavioral_artifacts("", "empty.rs").errors == []
    registry = ValidatorRegistry.with_builtin_validators()
    assert isinstance(registry.get("src/lib.rs"), type(validator))


def test_references_only_come_from_tests_and_assertion_macros():
    refs = _references("""
        use demo::{render, Widget};
        fn unused() { absent(); }
        #[test] fn checks() {
            // fake();
            let text = "pretend()";
            assert_eq!(render(), 2);
            let widget: Widget = Widget::new(3);
            assert_eq!(widget.read(), 3);
        }
    """)
    assert ("function", "render") in refs
    assert ("class", "Widget") in refs
    assert ("method", "Widget.new") in refs
    assert ("method", "Widget.read") in refs
    assert ("function", "absent") not in refs
    assert ("function", "fake") not in refs
    assert ("function", "pretend") not in refs


def test_aliases_and_inline_super_references_keep_original_identity():
    refs = _references("""
        use demo::{render as paint, Widget as View};
        pub fn render() -> u32 { 2 }
        #[cfg(test)] mod tests {
            use super::*;
            #[test] fn checks() { assert_eq!(super::render(), 2); paint(); let _view: View = View::new(1); }
        }
    """)
    assert ("function", "render") in refs
    assert ("method", "Widget.new") in refs
    assert ("function", "paint") not in refs
    assert ("method", "View.new") not in refs


def test_explicit_receivers_and_shadowing_do_not_guess_owner_names():
    refs = _references("""
        use demo::{Widget, Other};
        #[test] fn checks() {
            let widget: Widget = todo!();
            widget.read();
            { let widget: Other = todo!(); widget.read(); }
            widget.read();
            let widget = unknown_factory();
            widget.read();
            mysterious.read();
        }
    """)
    assert ("method", "Widget.read") in refs
    assert ("method", "Other.read") in refs
    assert ("method", "mysterious.read") not in refs
    assert ("method", "widget.read") not in refs


def test_no_references_from_imports_uninvoked_closures_or_unknown_macros():
    refs = _references("""use demo::Widget;
        #[test] fn checks() { let unused = || Widget::new(1); custom!(Widget::new(2)); }
    """)
    assert ("method", "Widget.new") not in refs
    assert ("class", "Widget") not in refs


def test_same_file_functions_and_typed_struct_literals_are_references():
    refs = _references("""pub struct Widget { pub value: i32 }
        pub fn render() -> i32 { 1 }
        #[test] fn checks() { let w = Widget { value: render() }; assert_eq!(w.value, 1); }
    """)
    assert ("class", "Widget") in refs
    assert ("attribute", "Widget.value") in refs
    assert ("function", "render") in refs


def test_test_body_detection_ignores_comment_and_string_attributes():
    validator = _rust_validator()
    bodies = validator.get_test_function_bodies(
        """// #[test] fn fake() {}
        pub fn render() { let s = "#[test]"; }
        #[cfg(test)] mod tests { #[test] fn checks() { assert_eq!(1, 1); } }
    """,
        "src/lib.rs",
    )
    assert set(bodies) == {"tests::checks"}
    assert "assert_eq!" in bodies["tests::checks"]


def test_module_dependency_paths_are_source_local_and_missing_modules_are_visible(
    tmp_path: Path,
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/math.rs").write_text("pub fn add() {}")
    validator = _rust_validator()
    result = validator.collect_dependencies(
        "mod math; mod missing;", "src/lib.rs", tmp_path
    )
    assert result.modules == ("src/math.rs",)
    assert result.unresolved
    assert result.supported


def test_cfg_and_macro_generated_modules_report_uncertainty(tmp_path: Path):
    validator = _rust_validator()
    result = validator.collect_dependencies(
        '#[cfg(feature = "extra")] mod extra;', "src/lib.rs", tmp_path
    )
    assert result.unresolved


def test_generic_associated_calls_and_reference_annotations_resolve_receivers():
    refs = _references("""use demo::Widget;
        #[test] fn checks() {
            let mut w: &Widget = todo!();
            w.read();
            let v: Widget<u32> = Widget::<u32>::new(1);
            v.read();
        }
    """)
    assert ("method", "Widget.new") in refs
    assert ("method", "Widget.read") in refs


@pytest.mark.parametrize(
    "binding",
    [
        "for widget in values { widget.read(); }",
        "if let Some(widget) = value { widget.read(); }",
        "match value { Some(widget) => widget.read(), _ => () };",
        "let (widget, _) = unknown(); widget.read();",
    ],
)
def test_pattern_bindings_shadow_outer_receivers(binding):
    refs = _references(
        "use demo::Widget; #[test] fn checks() { let widget: Widget = todo!(); "
        + binding
        + " }"
    )
    assert ("method", "Widget.read") not in refs


def test_child_modules_do_not_inherit_parent_imports_without_super_use():
    refs = _references(
        "use demo::render; mod tests { #[test] fn checks() { render(); } }"
    )
    assert ("function", "render") not in refs


def test_block_local_function_shadows_imported_function():
    refs = _references(
        "use demo::render; #[test] fn checks() { fn render() {} render(); }"
    )
    assert ("function", "render") not in refs


def test_module_and_function_namespace_imports_are_not_method_owners():
    refs = _references("use demo::math; #[test] fn checks() { math::render(); }")
    assert ("method", "math.render") not in refs


def test_capitalized_namespace_is_not_assumed_to_be_a_type():
    refs = _references(
        "use demo::Namespace; #[test] fn checks() { Namespace::render(); }"
    )
    assert ("method", "Namespace.render") not in refs
    assert ("class", "Namespace") not in refs


def test_new_method_does_not_imply_its_return_type():
    refs = _references(
        "use demo::Widget; #[test] fn checks() { let value = Widget::new(); value.read(); }"
    )
    assert ("method", "Widget.read") not in refs


def test_private_type_methods_are_not_public_snapshots():
    snapshot = _rust_validator().generate_snapshot(
        "struct Hidden; impl Hidden { pub fn run(&self) {} }", "src/lib.rs"
    )
    assert snapshot == []


def test_path_attribute_in_non_root_module_is_relative_to_its_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/alternate.rs").write_text("")
    result = _rust_validator().collect_dependencies(
        '#[path="alternate.rs"] mod inner;', "src/math.rs", tmp_path
    )
    assert result.modules == ("src/alternate.rs",)
    assert result.unresolved == ()


@pytest.mark.parametrize("attribute", ["#[ignore]", "#[cfg(any())]"])
def test_ignored_and_conditionally_disabled_tests_do_not_prove_calls(attribute):
    refs = _references(
        "pub fn render() {} #[test] " + attribute + " fn checks() { render(); }"
    )
    assert ("function", "render") not in refs


def test_conditional_parent_modules_do_not_prove_calls():
    refs = _references(
        "pub fn render() {} #[cfg(any())] mod tests { #[test] fn checks() { super::render(); } }"
    )
    assert ("function", "render") not in refs


def test_local_module_import_does_not_cover_same_named_root_function():
    refs = _references(
        "pub fn render() {} mod left { pub fn render() {} } use left::render as target; #[test] fn checks() { target(); }"
    )
    assert ("function", "render") not in refs
    assert ("function", "left::render") in refs


@pytest.mark.parametrize(
    "declaration",
    ["struct Widget;", "enum Widget { A }", "type Widget = u32;", "mod Widget {}"],
)
def test_block_local_types_and_modules_shadow_production_types(declaration):
    refs = _references(
        "pub struct Widget; #[test] fn checks() { " + declaration + " Widget::new(); }"
    )
    assert ("method", "Widget.new") not in refs
    assert ("class", "Widget") not in refs


def test_arbitrary_cargo_target_filename_uses_its_parent_for_mod_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/helper.rs").write_text("")
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    result = _rust_validator().collect_dependencies(
        "mod helper;", "tests/integration.rs", tmp_path
    )
    assert result.modules == ("tests/helper.rs",)
    assert result.unresolved == ()


@pytest.mark.parametrize("qualifier", ["crate", "self", "super"])
def test_qualified_associated_calls_use_known_type_owners(qualifier):
    prefix = "pub struct Widget; "
    body = "#[test] fn checks() { " + qualifier + "::Widget::new(); }"
    if qualifier == "super":
        body = "mod tests { use super::*; " + body + " }"
    refs = _references(prefix + body)
    assert ("method", "Widget.new") in refs


def test_direct_calls_through_local_modules_keep_qualified_function_identity():
    refs = _references(
        "pub mod left { pub fn render() {} } #[test] fn checks() { left::render(); }"
    )
    assert ("function", "left::render") in refs


def test_if_let_else_branch_uses_outer_typed_receiver():
    refs = _references(
        "use demo::Widget; #[test] fn checks() { let widget: Widget = todo!(); if let Some(widget) = value { let _ = widget; } else { widget.read(); } }"
    )
    assert ("method", "Widget.read") in refs


def test_explicit_self_parameter_is_omitted_from_method_arguments():
    artifacts = _definitions(
        "pub struct Widget; impl Widget { pub fn read(self: &Self) -> u32 { 1 } }"
    )
    assert artifacts[("method", "Widget.read")].args == ()


def test_split_module_imports_carry_crate_and_file_identity(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub mod math;")
    (tmp_path / "src/math.rs").write_text("pub fn add() {}")
    path = tmp_path / "tests/integration.rs"
    path.write_text("use demo::math::add; #[test] fn checks() { add(); }")
    validator = _rust_validator()
    assert validator.module_path("src/math.rs", tmp_path) == "demo.math"
    assert validator.module_path("src/lib.rs", tmp_path) == "demo"
    refs = validator.collect_behavioral_artifacts(path.read_text(), path).artifacts
    assert any(a.name == "add" and a.import_source == "demo.math" for a in refs)


def test_foreign_crate_function_does_not_cover_local_function(tmp_path):
    from maid_runner.core.identity import match_artifact_to_references
    from maid_runner.core.types import ArtifactKind
    from maid_runner.validators.base import FoundArtifact

    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    path = tmp_path / "src/lib.rs"
    source = "pub fn render() {} #[test] fn checks() { use other::render; render(); }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    declared = FoundArtifact(
        kind=ArtifactKind.FUNCTION, name="render", module_path="demo"
    )
    assert not match_artifact_to_references(declared, refs, tmp_path)


def test_runner_validates_split_module_imports_from_another_working_directory(tmp_path):
    from maid_runner.core.validate import validate

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "manifests").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nedition="2021"\n'
    )
    (tmp_path / "src/lib.rs").write_text("pub mod math;")
    (tmp_path / "src/math.rs").write_text("pub fn add() {}")
    (tmp_path / "tests/render.rs").write_text(
        "use demo::math::add; #[test] fn checks() { add(); }"
    )
    manifest = tmp_path / "manifests/rust.manifest.yaml"
    manifest.write_text(
        'schema: "2"\ngoal: Exercise split Rust module imports\ntype: feature\ncreated: "2026-10-02T00:00:00Z"\nfiles:\n  edit:\n    - path: src/math.rs\n      artifacts:\n        - {kind: function, name: add, args: [], returns: "()"}\n  read: [tests/render.rs]\nvalidate:\n  - [cargo, test, --offline]\n'
    )
    result = validate(manifest, project_root=tmp_path, mode="behavioral")
    assert result.success, result.errors


def test_disabled_declarations_and_statements_are_not_artifact_evidence():
    artifacts = _definitions("#[cfg(any())] pub fn render() {}")
    assert ("function", "render") not in artifacts
    refs = _references(
        "pub fn render() {} #[test] fn checks() { #[cfg(any())] render(); assert!(true); }"
    )
    assert ("function", "render") not in refs


@pytest.mark.parametrize(
    "source",
    [
        "use other::render as imported; #[test] fn checks() { { use demo::render as unused; } imported(); }",
        "use other::render; #[test] fn first() { use demo::render; assert!(true); } #[test] fn second() { render(); }",
    ],
)
def test_import_origins_do_not_leak_between_blocks_or_tests(tmp_path, source):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub fn render() {}")
    path = tmp_path / "tests/api.rs"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    calls = [a for a in refs if a.name == "render"]
    assert calls
    assert all(a.import_source == "other" for a in calls)


@pytest.mark.parametrize("qualification", ["crate", "super"])
def test_split_file_root_and_parent_qualifiers_do_not_become_current_module_calls(
    tmp_path, qualification
):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub fn render() {} pub mod math;")
    path = tmp_path / "src/math.rs"
    source = (
        "pub fn render() {} #[test] fn checks() { " + qualification + "::render(); }"
    )
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "render" and a.import_source == "demo" for a in refs)
    assert not any(a.name == "render" and a.import_source == "demo.math" for a in refs)


def test_disabled_automatic_test_targets_remain_modules_of_explicit_roots(tmp_path):
    (tmp_path / "tests/support").mkdir(parents=True)
    (tmp_path / "tests/integration.rs").write_text("mod support;")
    (tmp_path / "tests/support.rs").write_text("mod inner;")
    (tmp_path / "tests/support/inner.rs").write_text("#[test] fn checks() {}")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nautotests=false\n[[test]]\nname="integration"\npath="tests/integration.rs"\n'
    )
    result = _rust_validator().collect_dependencies(
        "mod inner;", "tests/support.rs", tmp_path
    )
    assert result.modules == ("tests/support/inner.rs",)
    assert result.unresolved == ()


@pytest.mark.parametrize(
    "declaration,call,kind,name",
    [
        ("pub enum State { Ready(u32) }", "State::Ready(1)", "enum", "State"),
        ("pub enum State { Ready }", "State::Ready", "enum", "State"),
        (
            "pub enum State { Ready { value: u32 } }",
            "State::Ready { value: 1 }",
            "enum",
            "State",
        ),
        ("pub struct Widget(pub u32);", "Widget(1)", "class", "Widget"),
    ],
)
def test_rust_constructors_preserve_their_declared_artifact_kind(
    declaration, call, kind, name
):
    refs = _references(declaration + " #[test] fn checks() { let _ = " + call + "; }")
    assert (kind, name) in refs


def test_simultaneous_import_aliases_keep_distinct_crate_identities(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub fn render() {}")
    path = tmp_path / "tests/api.rs"
    source = "use other::render as first; use demo::render as second; #[test] fn checks() { first(); }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "render" and a.import_source == "other" for a in refs)
    assert not any(a.name == "render" and a.import_source == "demo" for a in refs)


@pytest.mark.parametrize(
    "source",
    [
        "#![cfg(any())] pub fn render() {} #[test] fn checks() { render(); }",
        "pub mod disabled { #![cfg(any())] pub fn render() {} #[test] fn checks() { render(); } }",
    ],
)
def test_inner_conditional_attributes_disable_container_evidence(source):
    assert _definitions(source) == {}
    assert not any(name.endswith("render") for _, name in _references(source))
    assert _rust_validator().get_test_function_bodies(source, "file.rs") == {}


@pytest.mark.parametrize("macro", ["assert", "assert_eq", "assert_ne"])
def test_shadowed_assertion_macros_do_not_expand_as_standard_assertions(macro):
    source = (
        "pub fn render() {} macro_rules! "
        + macro
        + " { ($($t:tt)*) => {}; } #[test] fn checks() { "
        + macro
        + "!(render(), render()); }"
    )
    assert ("function", "render") not in _references(source)


def test_unpolled_async_blocks_do_not_prove_calls():
    refs = _references(
        "pub fn render() {} #[test] fn checks() { let _future = async { render(); }; assert!(true); }"
    )
    assert ("function", "render") not in refs


@pytest.mark.parametrize(
    "source,kind,name",
    [
        ("pub enum State { Ready }", "enum", "State"),
        ("pub enum State { Other { value: u32 } }", "enum", "State"),
        ("pub struct Widget(pub u32);", "class", "Widget"),
    ],
)
def test_imported_constructor_kinds_come_from_project_declarations(
    tmp_path, source, kind, name
):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text(source)
    expression = (
        "Widget(1)"
        if name == "Widget"
        else "State::Other { value: 1 }" if "Other" in source else "State::Ready"
    )
    path = tmp_path / "tests/api.rs"
    test = "use demo::" + name + "; #[test] fn checks() { let _ = " + expression + "; }"
    path.write_text(test)
    refs = _rust_validator().collect_behavioral_artifacts(test, path).artifacts
    assert any(
        a.kind.value == kind and a.name == name and a.import_source == "demo"
        for a in refs
    )


@pytest.mark.parametrize(
    "call", ["super::super::render();", "use super::super::render as target; target();"]
)
def test_repeated_parent_qualifiers_resolve_to_the_root_function(call):
    refs = _references(
        "pub fn render() {} mod outer { mod inner { #[test] fn checks() { "
        + call
        + " } } }"
    )
    assert ("function", "render") in refs


@pytest.mark.parametrize("qualifier", ["self", "crate"])
def test_qualified_inherent_impls_use_the_declared_owner(qualifier):
    definitions = _definitions(
        "pub struct Widget; impl " + qualifier + "::Widget { pub fn draw(&self) {} }"
    )
    assert ("method", "Widget.draw") in definitions


def test_nested_module_imports_keep_their_local_module_path():
    source = "pub mod outer { pub mod inner { pub fn render() {} } use inner::render as target; #[test] fn checks() { target(); } }"
    assert ("function", "outer::inner::render") in _references(source)


@pytest.mark.parametrize(
    "imports",
    [
        "use demo::math as module; use module::add as target;",
        "use module::add as target; use demo::math as module;",
    ],
)
def test_namespace_alias_import_chains_preserve_project_identity(tmp_path, imports):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub mod math;")
    (tmp_path / "src/math.rs").write_text("pub fn add() {}")
    path = tmp_path / "tests/api.rs"
    source = imports + " #[test] fn checks() { target(); }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "add" and a.import_source == "demo.math" for a in refs)


def test_function_or_value_named_assert_does_not_shadow_the_macro_namespace():
    source = "pub fn render() {} fn assert() {} #[test] fn checks() { let assert = 1; assert_eq!(assert, 1); assert!(render()); }"
    assert ("function", "render") in _references(source)


@pytest.mark.parametrize(
    "imports,call",
    [
        ("", "demo::render();"),
        ("use demo::{self as api, render};", "api::render();"),
        ("use demo::{self, render};", "demo::render();"),
    ],
)
def test_project_crate_qualifiers_and_group_self_imports_resolve(
    tmp_path, imports, call
):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub fn render() {}")
    path = tmp_path / "tests/api.rs"
    source = imports + " #[test] fn checks() { " + call + " }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "render" and a.import_source == "demo" for a in refs)


def test_value_named_std_does_not_shadow_the_standard_macro_namespace():
    source = "pub fn render() {} #[test] fn checks() { let std = 1; std::assert_eq!(std, 1); std::assert!(render()); }"
    assert ("function", "render") in _references(source)


def test_enum_variant_constructor_does_not_prove_same_named_inherent_method():
    refs = _references(
        "pub enum State { Ready(u32) } impl State { pub fn Ready(&self) -> u32 { 1 } } #[test] fn checks() { let _ = State::Ready(1); }"
    )
    assert ("enum", "State") in refs
    assert ("method", "State.Ready") not in refs


def test_nested_underscore_private_artifacts_keep_identity_and_stay_out_of_snapshot():
    source = "pub mod tools { fn _helper() {} struct _Widget; impl _Widget { pub fn draw(&self) {} } }"
    validator = _rust_validator()
    result = validator.collect_implementation_artifacts(source, "file.rs")
    assert any(a.name == "tools::_helper" and a.is_private for a in result.artifacts)
    assert validator.generate_snapshot(source, "file.rs") == []


def test_explicit_autobins_false_uses_cargos_implicit_main_path(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nautobins=false\n[[bin]]\nname="demo"\n'
    )
    (tmp_path / "src/lib.rs").write_text("pub fn render() {}")
    path = tmp_path / "src/main.rs"
    source = "fn main() {} pub fn render() {} #[test] fn checks() { crate::render(); }"
    path.write_text(source)
    validator = _rust_validator()
    module = validator.module_path(path, tmp_path)
    assert module == "demo.__bin__.demo"
    refs = validator.collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "render" and a.import_source == module for a in refs)
    assert not any(a.name == "render" and a.import_source == "demo" for a in refs)


@pytest.mark.parametrize(
    "scope,qualified",
    [("nested", "super::Widget"), ("outer::inner", "super::super::Widget")],
)
def test_parent_qualified_inherent_impls_use_the_declared_owner(scope, qualified):
    modules = scope.split("::")
    source = "pub struct Widget; " + "".join(
        "pub mod " + name + " {" for name in modules
    )
    source += "impl " + qualified + " { pub fn read(&self) {} }" + "}" * len(modules)
    assert ("method", "Widget.read") in _definitions(source)


@pytest.mark.parametrize(
    "macro,arguments",
    [
        ("assert", "render()"),
        ("assert_eq", "render(), render()"),
        ("assert_ne", "render(), render()"),
    ],
)
def test_standard_macro_imports_work_without_a_cargo_package(macro, arguments):
    source = (
        "use std::"
        + macro
        + "; pub fn render() {} #[test] fn checks() {"
        + macro
        + "!("
        + arguments
        + "); }"
    )
    assert ("function", "render") in _references(source)


@pytest.mark.parametrize(
    "manifest,source,expected",
    [
        ("& str", "&str", True),
        ("Vec < u8 >", "Vec<u8>", True),
        ("&'a str", "&'b str", False),
        ("&mut str", "&str", False),
        ("u32", "i32", False),
        ("std::string::String", "String", False),
        ("()", "()", True),
        (None, "u32", True),
    ],
)
def test_type_comparison_preserves_rust_semantics(manifest, source, expected):
    assert _rust_validator().types_match(manifest, source) is expected


@pytest.mark.parametrize(
    "manifest,implementation",
    [("&'a mut str", "&'amut str"), ('[u8; b"a b".len()]', '[u8; b"ab".len()]')],
)
def test_type_comparison_preserves_token_boundaries_and_literal_contents(
    manifest, implementation
):
    assert not _rust_validator().types_match(manifest, implementation)


def test_inline_path_attribute_changes_its_child_module_directory(tmp_path):
    (tmp_path / "src/alternate").mkdir(parents=True)
    (tmp_path / "src/inline").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    source = '#[path="alternate"] pub mod inline { pub mod child; }'
    (tmp_path / "src/lib.rs").write_text(source)
    (tmp_path / "src/alternate/child.rs").write_text("pub fn render() {}")
    (tmp_path / "src/inline/child.rs").write_text("pub fn wrong() {}")
    validator = _rust_validator()
    deps = validator.collect_dependencies(source, "src/lib.rs", tmp_path)
    assert deps.modules == ("src/alternate/child.rs",)
    assert deps.unresolved == ()
    assert (
        validator.module_path("src/alternate/child.rs", tmp_path) == "demo.inline.child"
    )


@pytest.mark.parametrize(
    "assertion",
    [
        'assert!(true, "{}", render());',
        'assert_eq!(1, 1, "{}", render());',
        'assert_ne!(1, 2, "{}", render());',
    ],
)
def test_assertion_failure_messages_do_not_supply_eager_call_evidence(assertion):
    assert ("function", "render") not in _references(
        "pub fn render() {} #[test] fn checks() { " + assertion + " }"
    )


def test_test_only_members_and_public_tests_stay_out_of_production_snapshots():
    source = "pub struct Widget { #[cfg(test)] pub value: u32 } pub trait Draw { #[cfg(test)] fn test_only(&self); } impl Widget { #[cfg(test)] pub fn test_only(&self) {} } #[test] pub fn check() {}"
    snapshot = _rust_validator().generate_snapshot(source, "file.rs")
    assert snapshot == [
        {"kind": "class", "name": "Widget"},
        {"kind": "interface", "name": "Draw"},
    ]


def test_generic_receiver_calls_reference_their_declared_method():
    refs = _references(
        "pub struct Widget; #[test] fn checks() { let w: Widget = Widget; w.draw::<u32>(); }"
    )
    assert ("method", "Widget.draw") in refs


def test_loop_iterator_expression_uses_the_outer_receiver_binding():
    refs = _references(
        "pub struct Widget; #[test] fn checks() { let w: Widget = Widget; for w in w.draw() { let _ = w; } }"
    )
    assert ("method", "Widget.draw") in refs


@pytest.mark.parametrize(
    "source",
    [
        "pub fn render() {} #[test] fn checks() { assert_eq!(render(), 0); macro_rules! assert_eq { ($($t:tt)*) => {}; } }",
        "pub fn render() {} #[test] fn checks() { assert_eq!(render(), 0); } macro_rules! assert_eq { ($($t:tt)*) => {}; }",
    ],
)
def test_later_macro_declarations_do_not_shadow_earlier_standard_assertions(source):
    assert ("function", "render") in _references(source)


def test_unit_struct_constructor_identifier_is_a_class_reference():
    assert ("class", "Widget") in _references(
        "pub struct Widget; #[test] fn checks() { let _widget = Widget; }"
    )


def test_imported_unit_struct_constructor_keeps_its_class_kind(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub struct Widget;")
    path = tmp_path / "tests/api.rs"
    source = "use demo::Widget; #[test] fn checks() { let _widget = Widget; }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(
        a.kind.value == "class" and a.name == "Widget" and a.import_source == "demo"
        for a in refs
    )


@pytest.mark.parametrize(
    "declaration", ["const run: fn() = || {};", "static run: fn() = || {};"]
)
def test_local_constants_and_statics_shadow_production_function_bindings(declaration):
    source = "pub fn run() {} #[test] fn checks() { " + declaration + " run(); }"
    assert ("function", "run") not in _references(source)


def test_value_binding_does_not_shadow_the_type_namespace():
    source = "pub struct Widget { pub value: u32 } impl Widget { pub fn new() -> Self { todo!() } } #[test] fn checks() { let Widget = 1; let _ = Widget::new(); }"
    refs = _references(source)
    assert ("class", "Widget") in refs
    assert ("method", "Widget.new") in refs


@pytest.mark.parametrize(
    "import_statement,call", [("use ::demo::run;", "run();"), ("", "::demo::run();")]
)
def test_absolute_crate_paths_preserve_their_module_identity(
    tmp_path, import_statement, call
):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub fn run() {}")
    path = tmp_path / "tests/api.rs"
    source = import_statement + " #[test] fn checks() { " + call + " }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "run" and a.import_source == "demo" for a in refs)


def test_named_struct_and_function_keep_separate_value_namespace_identity():
    source = "pub struct Widget { pub value: u32 } pub fn Widget() -> u32 { 1 } #[test] fn checks() { let _ = Widget(); }"
    refs = _references(source)
    assert ("function", "Widget") in refs
    assert ("class", "Widget") not in refs


@pytest.mark.parametrize(
    "declaration", ["const run: fn() = || {};", "static run: fn() = || {};"]
)
def test_module_constants_and_statics_shadow_parent_function_imports(declaration):
    source = (
        "pub fn run() {} mod tests { use super::*; "
        + declaration
        + " #[test] fn checks() { run(); } }"
    )
    assert ("function", "run") not in _references(source)


def test_qualified_impls_for_private_module_types_stay_out_of_public_snapshots():
    source = "mod private { pub struct Hidden; } impl private::Hidden { pub fn call(&self) {} }"
    assert _rust_validator().generate_snapshot(source, "file.rs") == []


def test_qualified_private_traits_do_not_export_their_impl_methods():
    source = "pub struct Widget; pub mod detail { pub(crate) trait Hidden { fn call(&self); } } impl detail::Hidden for Widget { fn call(&self) {} }"
    assert _rust_validator().generate_snapshot(source, "file.rs") == [
        {"kind": "class", "name": "Widget"}
    ]


def test_let_chain_receiver_bindings_do_not_reuse_outer_type_identity():
    source = "pub struct Widget; #[test] fn checks() { let x: Widget = Widget; if let Some(x) = value && true { x.read(); } }"
    assert ("method", "Widget.read") not in _references(source)


def test_let_chain_function_bindings_shadow_production_function_names():
    source = "pub fn target() {} #[test] fn checks() { if let Some(target) = value && true { target(); } }"
    assert ("function", "target") not in _references(source)


def test_qualified_unit_struct_constructor_retains_its_class_identity():
    assert ("class", "m::Widget") in _references(
        "pub mod m { pub struct Widget; } #[test] fn checks() { let _ = m::Widget; }"
    )


def test_import_aliases_do_not_expose_private_types_or_traits():
    sources = [
        "struct Hidden; use Hidden as Alias; impl Alias { pub fn read(&self) {} }",
        "trait Hidden { fn read(&self); } use Hidden as Alias; pub struct Widget; impl Alias for Widget { fn read(&self) {} }",
    ]
    validator = _rust_validator()
    assert validator.generate_snapshot(sources[0], "file.rs") == []
    assert validator.generate_snapshot(sources[1], "file.rs") == [
        {"kind": "class", "name": "Widget"}
    ]


def test_public_import_aliases_use_the_original_impl_owner():
    artifacts = _definitions(
        "pub struct Widget; use Widget as Alias; impl Alias { pub fn read(&self) {} }"
    )
    assert ("method", "Widget.read") in artifacts


@pytest.mark.parametrize("pattern", ["ref w", "ref mut w"])
def test_reference_pattern_receivers_preserve_explicit_type_annotations(pattern):
    source = (
        "pub struct Widget; #[test] fn checks() { let "
        + pattern
        + ": Widget = Widget; w.read(); }"
    )
    assert ("method", "Widget.read") in _references(source)


def test_parenthesized_function_callee_preserves_function_reference():
    assert ("function", "render") in _references(
        "pub fn render() {} #[test] fn checks() { (render)(); }"
    )


def test_parenthesized_callable_field_is_an_attribute_reference():
    refs = _references(
        "pub struct Widget { pub read: fn() } #[test] fn checks() { let w: Widget = todo!(); (w.read)(); }"
    )
    assert ("attribute", "Widget.read") in refs
    assert ("method", "Widget.read") not in refs


@pytest.mark.parametrize("path", ["self::Widget", "crate::Widget", "m::Widget"])
def test_qualified_tuple_struct_calls_keep_the_class_kind(path):
    source = (
        "pub struct Widget(pub u32); "
        if path != "m::Widget"
        else "pub mod m { pub struct Widget(pub u32); } "
    )
    refs = _references(source + "#[test] fn checks() { let _ = " + path + "(1); }")
    expected = "m::Widget" if path == "m::Widget" else "Widget"
    assert ("class", expected) in refs
    assert ("function", expected) not in refs


def test_same_file_function_import_alias_keeps_project_module_identity(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    path = tmp_path / "src/lib.rs"
    source = "pub fn render() {} use render as alias; #[test] fn checks() { alias(); }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "render" and a.import_source == "demo" for a in refs)
    assert not any(a.name == "render" and a.import_source is None for a in refs)


@pytest.mark.parametrize("variant,call", [("V(u32)", "V(1)"), ("V", "V")])
def test_imported_enum_variants_keep_parent_enum_evidence(variant, call):
    source = (
        "pub enum E { "
        + variant
        + " } use crate::E::V; #[test] fn checks() { let _ = "
        + call
        + "; }"
    )
    assert ("enum", "E") in _references(source)


def test_imported_type_alias_struct_literals_keep_the_type_kind():
    source = "pub mod types { pub struct S { pub x: u32 } pub type T = S; } use crate::types::T; #[test] fn checks() { let _ = T { x: 1 }; }"
    refs = _references(source)
    assert ("type", "types::T") in refs
    assert ("class", "types::T") not in refs


def test_extern_crate_alias_does_not_resolve_to_the_project_crate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    (tmp_path / "src/lib.rs").write_text("pub fn render() {}")
    path = tmp_path / "tests/api.rs"
    source = "extern crate other as demo; #[test] fn checks() { demo::render(); }"
    path.write_text(source)
    refs = _rust_validator().collect_behavioral_artifacts(source, path).artifacts
    assert any(a.name == "render" and a.import_source == "other" for a in refs)
    assert not any(a.name == "render" and a.import_source == "demo" for a in refs)


def test_extern_crate_std_alias_does_not_expand_custom_assertions():
    source = "extern crate demo as std; pub fn render() {} #[test] fn checks() { std::assert_eq!(render(), 1); }"
    assert ("function", "render") not in _references(source)


@pytest.mark.parametrize(
    "import_path", ["crate::model::Widget", "super::model::Widget"]
)
def test_split_file_inherent_methods_match_explicit_receiver_references(
    tmp_path, import_path
):
    from dataclasses import replace

    from maid_runner.core.identity import match_artifact_to_references

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nedition="2021"\n'
    )
    (tmp_path / "src/lib.rs").write_text("pub mod model; mod actions;")
    (tmp_path / "src/model.rs").write_text("pub struct Widget;")
    implementation = tmp_path / "src/actions.rs"
    implementation.write_text(
        f"use {import_path}; impl Widget {{ pub fn run(&self) {{}} }}"
    )
    test = tmp_path / "tests/api.rs"
    test.write_text(
        "use demo::model::Widget; #[test] fn checks() { let w: Widget = Widget; w.run(); }"
    )
    validator = _rust_validator()
    definitions = validator.collect_implementation_artifacts(
        implementation.read_text(), implementation
    ).artifacts
    method = next(a for a in definitions if a.name == "run")
    assert method.of == "crate::model::Widget"
    identity = replace(
        method, module_path=validator.module_path(implementation, tmp_path)
    )
    refs = validator.collect_behavioral_artifacts(test.read_text(), test).artifacts
    assert match_artifact_to_references(identity, refs, tmp_path)
    assert not match_artifact_to_references(
        replace(identity, of="unrelated::Widget"), refs, tmp_path
    )


def test_hyphenated_explicit_binary_uses_implicit_main_identity(tmp_path):
    from maid_runner.core.identity import match_artifact_to_references
    from maid_runner.core.types import ArtifactKind
    from maid_runner.validators.base import FoundArtifact

    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="my-app"\nversion="0.1.0"\nautobins=false\n[[bin]]\nname="my-app"\n'
    )
    path = tmp_path / "src/main.rs"
    source = "pub fn render() {} #[test] fn checks() { crate::render(); }"
    path.write_text(source)
    validator = _rust_validator()
    module = validator.module_path(path, tmp_path)
    assert module == "my_app.__bin__.my-app"
    refs = validator.collect_behavioral_artifacts(source, path).artifacts
    assert match_artifact_to_references(
        FoundArtifact(kind=ArtifactKind.FUNCTION, name="render", module_path=module),
        refs,
        tmp_path,
    )


@pytest.mark.parametrize(
    "source",
    [
        'mod condition { #![cfg(feature="off")] #[test] fn checks() {} }',
        '#![cfg(feature="off")] #[test] fn checks() {}',
    ],
)
def test_inner_conditional_attributes_report_dependency_uncertainty(tmp_path, source):
    path = tmp_path / "tests/conditional.rs"
    path.parent.mkdir()
    path.write_text(source)
    result = _rust_validator().collect_dependencies(source, path, tmp_path)
    assert result.supported
    assert result.unresolved
    assert not result.modules


@pytest.mark.parametrize(
    "literal", ['r#"alternate.rs"#', 'r##"alternate.rs"##', '"alternate\\u{2e}rs"']
)
def test_rust_path_literals_resolve_loaded_file_identity(tmp_path, literal):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"\n')
    path = tmp_path / "src/lib.rs"
    source = "#[path=" + literal + "] pub mod inner;"
    path.write_text(source)
    (tmp_path / "src/alternate.rs").write_text("pub fn render() {}")
    validator = _rust_validator()
    result = validator.collect_dependencies(source, path, tmp_path)
    assert not result.unresolved, result.unresolved
    assert result.modules == ("src/alternate.rs",)
    assert validator.module_path("src/alternate.rs", tmp_path) == "demo.inner"


def test_raw_string_inline_path_overrides_resolve_descendant_modules(tmp_path):
    (tmp_path / "src/custom").mkdir(parents=True)
    path = tmp_path / "src/lib.rs"
    source = '#[path=r#"custom"#] pub mod inner { mod child; }'
    path.write_text(source)
    (tmp_path / "src/custom/child.rs").write_text("pub fn render() {}")
    result = _rust_validator().collect_dependencies(source, path, tmp_path)
    assert not result.unresolved
    assert result.modules == ("src/custom/child.rs",)


def test_workspace_library_outside_package_preserves_identity_and_rejects_other_crate(
    tmp_path,
):
    from maid_runner.core.validate import validate

    for folder in ("demo/tests", "other/src", "shared", "manifests"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers=["demo", "other"]\nresolver="2"\n'
    )
    (tmp_path / "demo/Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\nedition="2021"\n[lib]\npath="../shared/lib.rs"\n[dependencies]\nother={path="../other"}\n'
    )
    (tmp_path / "other/Cargo.toml").write_text(
        '[package]\nname="other"\nversion="0.1.0"\nedition="2021"\n'
    )
    (tmp_path / "other/src/lib.rs").write_text("pub fn render() -> u32 { 1 }")
    (tmp_path / "shared/lib.rs").write_text("pub fn render() -> u32 { 1 } mod math;")
    (tmp_path / "shared/math.rs").write_text("pub fn add() {}")
    test = tmp_path / "demo/tests/api.rs"
    test.write_text("#[test] fn checks() { assert_eq!(other::render(), 1); }")
    validator = _rust_validator()
    assert validator.module_path("shared/lib.rs", tmp_path) == "demo"
    assert validator.module_path("shared/math.rs", tmp_path) == "demo.math"
    manifest = tmp_path / "manifests/api.manifest.yaml"
    manifest.write_text("""schema: "2"
type: feature
goal: Check shared library identity
created: "2026-10-02T00:00:00Z"
files:
  edit:
    - path: shared/lib.rs
      artifacts: [{kind: function, name: render, args: [], returns: u32}]
  read: [demo/tests/api.rs]
validate: [[cargo, test, --offline, -p, demo]]
""")
    for mode in ("behavioral", "implementation"):
        result = validate(manifest, project_root=tmp_path, mode=mode)
        assert not result.success
        assert any(e.code.value == "E200" for e in result.errors), result.errors
    test.write_text("#[test] fn checks() { assert_eq!(demo::render(), 1); }")
    for mode in ("behavioral", "implementation"):
        result = validate(manifest, project_root=tmp_path, mode=mode)
        assert result.success, result.errors
