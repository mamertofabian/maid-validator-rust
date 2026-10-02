from maid_runner.testing.validator_conformance import (
    ConformanceArtifactSample,
    ConformanceFixtures,
    make_conformance_suite,
)


def test_public_validator_conformance_kit():
    from maid_validator_rust import RustValidator

    samples = {
        "class": ConformanceArtifactSample("pub struct Widget;", "Widget"),
        "function": ConformanceArtifactSample("pub fn render() {}", "render"),
        "method": ConformanceArtifactSample(
            "impl Widget { pub fn draw(&self) {} }", "draw", "Widget"
        ),
        "attribute": ConformanceArtifactSample(
            "pub struct Widget { pub value: u32 }", "value", "Widget"
        ),
        "interface": ConformanceArtifactSample("pub trait Draw {}", "Draw"),
        "enum": ConformanceArtifactSample("pub enum State { Ready }", "State"),
        "type": ConformanceArtifactSample("pub type Label = String;", "Label"),
    }
    fixtures = ConformanceFixtures(
        extension=".rs",
        artifact_samples=samples,
        private_artifact_source="fn _helper() {}",
        behavioral_target_kind="function",
        behavioral_target_name="render",
        behavioral_target_of=None,
        behavioral_correct_source="use demo::render; #[test] fn checks() { render(); }",
        behavioral_wrong_identity_source="use demo::other; #[test] fn checks() { other(); }",
        unparseable_source="pub fn {",
        empty_source="",
    )
    suite = make_conformance_suite(RustValidator, fixtures)()
    for kind, sample in samples.items():
        suite.test_collects_declared_implementation_artifact(kind, sample)
        suite.test_declared_artifact_identity_fields_are_exact(kind, sample)
        suite.test_collecting_same_sample_is_deterministic(kind, sample)
    suite.test_private_artifacts_stay_private_and_out_of_snapshot()
    suite.test_behavioral_sample_references_declared_target()
    suite.test_wrong_identity_behavioral_sample_does_not_match_target()
    suite.test_unparseable_source_reports_errors_without_artifacts()
    suite.test_empty_source_returns_no_artifacts_or_errors()
    # Rust visibility extends beyond the name-prefix privacy convention.
    snapshot = RustValidator().generate_snapshot(
        "pub struct Widget; trait Hidden { fn secret(&self); } "
        "impl Hidden for Widget { fn secret(&self) {} }",
        "src/lib.rs",
    )
    assert snapshot == [{"kind": "class", "name": "Widget"}]
