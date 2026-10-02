# Releasing

This prepared release route follows `maid-validator-csharp` and
`maid-validator-solidity`: Python 3.10-3.14 CI, SHA-pinned actions, pinned build
and metadata tools, isolated wheel discovery, PyPI Trusted Publisher OIDC and
GitHub release assets. Version 0.1.0 is unpublished. Local preparation creates
neither the remote repository nor the external publisher binding.

## One-time setup

Create the public GitHub repository `mamertofabian/maid-validator-rust` and connect
this checkout. Create a `pypi` GitHub environment, restrict deployments to release
tags (`v*`), and apply the intended approvals before publishing.

In the PyPI account's Publishing settings, add a pending publisher with:

- PyPI project name: `maid-validator-rust`
- GitHub owner: `mamertofabian`
- GitHub repository: `maid-validator-rust`
- Workflow filename: `publish.yml`
- Environment name: `pypi`

The first successful OIDC publication creates the PyPI project. Use the
short-lived token supplied by the publish action; no PyPI API token secret is
needed. Only the publish job has `id-token: write`; only the GitHub release job
has `contents: write`.

## Runner compatibility

Plugin collection, snapshots and discovery use the verified published API floor
**2.27.6**. The package and release lock are index-only; never tag with a local
`[tool.uv.sources]` override.

Full Rust MAID validation requires **2.27.7+** for `.rs`/inline test discovery,
Cargo command integrity and exact reference identity. That Runner is not yet
published. Release the plugin independently for collection/discovery now; full
Rust project validation becomes available when the Rust-enabled Runner is released.

On 2.27.6, the four full integration cases are explicitly skipped. Collector,
conformance and release tests stay active. Validate those integration assertions
against the local Rust-enabled 2.27.7 checkout in a separate environment before
this release. When the published dependency is upgraded, the same cases run
automatically. Do not describe a 2.27.6 installation as full Cargo validation.

## Release checklist

1. Complete the external setup above. Start from clean,
   synchronized `main`; confirm the version and tag are absent on PyPI and GitHub.
2. Check `project.version`, date the matching CHANGELOG.md entry, and retain
   unpublished status until actual publication succeeds.
3. Run the index-only gates:

   ```bash
   uv lock --check
   uv sync --locked
   cargo --version
   uv run pytest -q
   uv run ruff check src/ tests/
   uv run black --check src/ tests/
   uv run maid validate
   uv run maid test
   uv run maid assess --since <baseline>
   # Run the exact verify command emitted by assess.
   ```

4. Build fresh wheel and source distributions, check their metadata, then install
   the actual wheel in an isolated environment:

   ```bash
   release_dir=$(mktemp -d)
   uv build --out-dir "$release_dir"
   uvx --from twine==7.0.0 twine check "$release_dir"/*
   uv venv --python 3.10 "$release_dir/smoke"
   uv pip install --python "$release_dir/smoke/bin/python" "$release_dir"/*.whl
   "$release_dir/smoke/bin/maid" validators --json
   ```

   Require active `RustValidator`, extension `.rs`, and source
   `maid-validator-rust <version>`. Exercise Python 3.10-3.14 and confirm both
   distribution formats contain the intended package, MIT license and metadata.
   Also test the unchanged full-integration assertions against local Runner
   2.27.7. Those tests supplement this public-index installation gate.
5. Complete independent MAID implementation review and capture Outcome. Obtain
   explicit approval for the release commit and separate approval to push it.
   Wait for branch CI to pass before proceeding to a release tag.
6. Recheck main ancestry, version, tag absence and publisher/environment settings.
   Obtain separate approval to create and push the matching annotated tag:

   ```bash
   git tag -a v0.1.0 -m "Release maid-validator-rust 0.1.0"
   git push origin v0.1.0
   ```

The `v*` workflow tests every supported Python version, rejects development
sources, proves the tag belongs to main and matches `project.version`, builds
and checks distributions, verifies installed-wheel discovery, publishes through
`pypi`, and attaches those same distributions to a GitHub Release.

## Post-release verification

Install from the public index in a fresh environment:

```bash
uvx --from maid-validator-rust maid validators --json
```

Confirm active Rust discovery, the intended PyPI/GitHub versions and matching
artifact hashes. Update publication status only after verification. If PyPI
accepts a version but a later step fails, complete the missing step without
moving the tag or attempting to republish the immutable version.
