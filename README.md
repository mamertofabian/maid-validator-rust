# MAID Rust Validator

Rust validator plugin for MAID Runner, using tree-sitter-rust for offline source collection.

This repository contains the initial, unpublished 0.1.0 implementation. Parsing
does not invoke Cargo, compile code, access the network, or expand macros.

## Development and installation

Python 3.10+ is supported. For development alongside the sibling Runner checkout:

```bash
uv sync --group dev
uv pip install --python .venv/bin/python --no-deps -e ../maid-runner
uv run --no-sync maid validators
uv run --no-sync python -m pytest tests/ -q
uv run --no-sync ruff check src/ tests/
uv run --no-sync black --target-version py310 --check src/ tests/
uv build
```

Use `--no-sync` while testing the sibling checkout: plain `uv run` restores the
published Runner version in `uv.lock`. The package can collect Rust with Runner
2.27.6+, but the Cargo/discovery integration requires the updated Runner checkout
until that support is released. To install locally into another Runner environment:

```bash
uv pip install --python /path/to/environment/bin/python /path/to/maid-validator-rust
```

The `maid_runner.validators` entry point discovers `RustValidator` for `.rs`.
Cargo is required for Runner's offline target metadata and executable Rust tests.

## Artifact mapping

| Rust declaration | MAID artifact |
|---|---|
| Public named struct | `class` |
| Public trait | `interface` |
| Public enum | `enum` |
| Public type alias | `type` |
| Public named struct field | `attribute` with its struct owner |
| Public free function | `function` |
| Public inherent method / trait implementation method | `method` with its type owner |

Inline public module names qualify identities with `::`. Restricted visibility
(`pub(crate)`, `pub(super)`) and private APIs are omitted from public snapshots.
Underscore private artifacts preserve the shared conformance convention.
Arguments preserve raw types and generics; `self` is omitted from method arguments;
no return is represented as `()`. Type comparison ignores whitespace while
preserving lifetimes, mutability and qualified names.

## Behavioral references

Standard `#[test]` bodies provide references, including inline `#[cfg(test)]`
modules. The collector handles source-visible imports and aliases, same-file
declarations, `super`/`self`/`crate` paths, struct literals, explicit receiver type
annotations, field access and associated calls with syntactic type evidence.
Assertions `assert!`, `assert_eq!` and `assert_ne!` (also `std::` qualified) have
their eager arguments parsed as Rust expressions; failure-message arguments do
not supply ordinary call evidence. Shadowed macros retain their lexical scope.
Imports, strings, comments, unused helpers, deferred closures/async blocks and
arbitrary macros do not count as calls.

Use explicit type annotations when a receiver's type would otherwise require
inference. A function named `new` need not return `Self`; the collector does not
assume it does. Imported names alone cannot distinguish modules from types.
Unknown bindings do not produce guessed method owners or exact signatures.
Ignored tests and test bodies with unresolved conditional attributes do not
prove behavioral calls. Local types/modules shadow production imports as well
as local value bindings. Rust references request exact kind and owner matching.
Declarations and statements with unresolved conditional attributes are likewise
excluded from artifact evidence; ordinary `#[cfg(test)]` unit-test scopes are
supported. Enum variants and tuple-struct constructors retain their parent
declaration's artifact kind.

For project files, module identity follows the nearest Cargo package and parsed
library module graph, including literal path overrides. Imported crate and
module identities distinguish `demo::math::add` from an unrelated crate's `add`.
Inline module artifacts retain their qualified names; split-file declarations
retain their local names with their file's module identity.
An inherent implementation in another file retains its physical file identity
and uses a crate-qualified owner, such as `of: crate::model::Widget`. Explicit
receiver references are also mapped to those source-defined implementations.
Workspace member target roots can point outside their package directory while
remaining inside the project root. Files without resolved package ownership use
their physical file identity, so another crate's same-named API cannot cover them.

## Cargo manifest example

```yaml
schema: "2"
goal: Preserve the public render function
type: feature
created: "2026-10-02T00:00:00Z"
files:
  edit:
    - path: src/lib.rs
      artifacts:
        - kind: function
          name: render
          args: []
          returns: u32
  read:
    - tests/render.rs
validate:
  - [cargo, test]
```

Runner supports ordinary Cargo targets, packages, workspaces and inline tests.
It follows only loaded `mod` files, using offline metadata without test compilation
during behavioral validation. `mod foo;`, inline modules and literal `#[path]`
overrides resolve to project-local paths. Missing/ambiguous modules, module-level
macro expansion and conditional modules other than `#[cfg(test)]` report explicit
uncertainty and do not prove complete file coverage. Metadata failure produces a
visible command-integrity failure when declared tests require execution.

## Initial support boundaries

- No compiler-dependent type binding, cross-file type inference, macro-generated
  API, re-export resolution or exact trait disambiguation.
- A source file reused under multiple `mod` names has one file-level identity;
  validation through every alias is outside this version.
- Reference collection does not model arbitrary control-flow reachability.
  Cargo target coverage proves the selected source targets, rather than runtime
  execution of each function. Use Rust coverage tooling for runtime evidence.
- Constants/statics, associated types, enum variants and tuple fields are not
  separate artifact declarations.
- Custom test harnesses, doctest references, attribute test frameworks such as
  `#[tokio::test]`, and indirect helper/closure call graphs are outside this version.
- Cargo name filters, `--no-run`, `--doc`, harness list/ignored/skip filters,
  feature/cfg mutation and unknown command options cannot prove full-file execution.
- Cargo's configured feature conditions require future compiler/configuration
  evidence; module uncertainty is reported rather than guessed.
  Targets with inactive `required-features` or custom `harness=false` are excluded
  from ordinary test-file coverage.

Malformed syntax returns errors and zero collected artifacts. The test suite runs
the public Runner conformance kit, entry-point discovery and adversarial identities.
