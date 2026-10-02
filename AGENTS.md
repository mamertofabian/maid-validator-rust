# Repository guidance

Use `uv` for Python environments. Source lives in `src/maid_validator_rust/`;
behavioral tests live in `tests/`. Run pytest, Ruff and Black before handoff.

Follow MAID: draft contract and tests, confirm red, validate behavioral scope,
lock and promote, implement within scope, validate and run independent read-only
review, then capture Outcome and refresh `maid learn`. Revise unmerged contracts
in place. Never weaken tests to hide unsupported Rust syntax or identities.

The sibling Runner checkout provides the Cargo integration under development.
Use `uv run --no-sync` after installing it into this environment as described in
README. Keep collectors offline; do not require compilation during behavioral
validation. Do not commit, push or publish without explicit user approval.
