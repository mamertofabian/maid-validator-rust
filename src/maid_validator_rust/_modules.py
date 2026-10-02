"""Cargo source roots and parser-backed file module identities."""

from __future__ import annotations

from pathlib import Path

from maid_validator_rust._parse import _parse


def _cargo_config(directory: Path) -> dict | None:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    try:
        return tomllib.loads((directory / "Cargo.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None


def _workspace_packages(
    directory: Path, config: dict, root: Path
) -> list[tuple[Path, dict]]:
    import fnmatch

    candidates = {directory} if "package" in config else set()
    workspace = config.get("workspace", {})
    for pattern in workspace.get("members", ()):
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            continue
        candidates.update(
            path.resolve() for path in directory.glob(pattern) if path.is_dir()
        )
    packages = []
    for candidate in sorted(candidates):
        if not candidate.is_relative_to(root):
            continue
        relative = candidate.relative_to(directory).as_posix()
        if any(
            fnmatch.fnmatchcase(relative, pattern)
            for pattern in workspace.get("exclude", ())
        ):
            continue
        member = config if candidate == directory else _cargo_config(candidate)
        if member and "package" in member:
            packages.append((candidate, member))
    return packages


def _package_info(file_path: Path, project_root: Path) -> tuple[Path, dict] | None:
    root = project_root.resolve()
    full = file_path.resolve()
    directory = full.parent
    nearest = None
    while directory.is_relative_to(root):
        config = _cargo_config(directory)
        if config:
            if "package" in config and nearest is None:
                nearest = directory, config
            if "workspace" in config:
                packages = _workspace_packages(directory, config, root)
                exact = [info for info in packages if full in _crate_roots(*info)]
                if exact:
                    return exact[0] if len(exact) == 1 else None
                # A configured root can load modules outside its package too.
                containing = [
                    info
                    for info in packages
                    if any(
                        full.is_relative_to(target.parent)
                        for target in _crate_roots(*info)
                    )
                ]
                if containing:
                    return containing[0] if len(containing) == 1 else nearest
        if directory == root:
            break
        directory = directory.parent
    return nearest


def _file_identity(path: Path) -> str:
    return "__rust_file__." + path.resolve().as_posix().replace("/", ".")


def _context_root(directory: Path) -> Path:
    for ancestor in (directory, *directory.parents):
        config = _cargo_config(ancestor)
        if config and "workspace" in config:
            return ancestor
    return directory


def _crate_roots(directory: Path, config: dict) -> dict[Path, str]:
    package = config["package"]["name"].replace("-", "_")
    lib = config.get("lib", {})
    library = lib.get("name", package)
    settings = config["package"]
    roots = {}
    if "lib" in config or settings.get("autolib", True):
        roots[(directory / lib.get("path", "src/lib.rs")).resolve()] = library
    if settings.get("autobins", True):
        roots[(directory / "src/main.rs").resolve()] = package + ".__bin__." + package
    for kind, folder in (
        ("bin", "src/bin"),
        ("test", "tests"),
        ("example", "examples"),
        ("bench", "benches"),
    ):
        automatic = settings.get(
            {
                "bin": "autobins",
                "test": "autotests",
                "example": "autoexamples",
                "bench": "autobenches",
            }[kind],
            True,
        )
        if automatic:
            for path in (directory / folder).glob("*.rs"):
                roots[path.resolve()] = package + ".__" + kind + "__." + path.stem
            for path in (directory / folder).glob("*/main.rs"):
                roots[path.resolve()] = (
                    package + ".__" + kind + "__." + path.parent.name
                )
        for declaration in config.get(kind, ()):
            name = declaration.get("name", Path(declaration.get("path", "")).stem)
            default = f"{folder}/{name}.rs"
            if (
                kind == "bin"
                and name == config["package"]["name"]
                and (directory / "src/main.rs").is_file()
            ):
                default = "src/main.rs"
            elif (
                not (directory / default).is_file()
                and (directory / folder / name / "main.rs").is_file()
            ):
                default = f"{folder}/{name}/main.rs"
            roots[(directory / declaration.get("path", default)).resolve()] = (
                package + ".__" + kind + "__." + name
            )
    return roots


def _is_crate_root(file_path: Path, project_root: Path) -> bool:
    info = _package_info(file_path, project_root)
    if info is None:
        return False
    directory, config = info
    if file_path.resolve() in _crate_roots(directory, config):
        return True
    if not file_path.resolve().parent.is_relative_to(directory):
        return False
    parent = file_path.resolve().parent.relative_to(directory).as_posix()
    flag = {
        "tests": "autotests",
        "examples": "autoexamples",
        "benches": "autobenches",
        "src/bin": "autobins",
    }.get(parent)
    return flag is not None and config["package"].get(flag, True)


def _module_map(file_path: Path, project_root: Path) -> dict[str, Path]:
    from maid_validator_rust._dependencies import _dependency_edges

    info = _package_info(file_path, project_root)
    if info is None:
        return {}
    modules: dict[str, Path] = {}
    for path, identity in _crate_roots(*info).items():
        if not path.is_file() or not path.is_relative_to(project_root.resolve()):
            continue
        pending = [(path, identity)]
        visited: set[Path] = set()
        while pending:
            current, name = pending.pop()
            if current in visited or not current.resolve().is_relative_to(
                project_root.resolve()
            ):
                continue
            visited.add(current)
            modules[name] = current
            encoded, parsed, errors = _parse(current.read_text(encoding="utf-8"))
            if parsed is None or errors:
                continue
            edges, _ = _dependency_edges(parsed, encoded, current, project_root)
            pending.extend(
                (project_root / target, name + "." + child.replace("::", "."))
                for child, target in edges
            )
    return modules


def _module_path(file_path: str | Path, project_root: Path) -> str | None:
    full = (project_root / file_path).resolve()
    for identity, path in _module_map(full, project_root).items():
        if path == full:
            return identity
    info = _package_info(full, project_root)
    if info is None:
        return _file_identity(full) if full.is_file() else None
    directory, config = info
    base = directory if full.is_relative_to(directory) else project_root.resolve()
    relative = full.relative_to(base).with_suffix("").as_posix().replace("/", ".")
    return config["package"]["name"].replace("-", "_") + ".__file__." + relative


def _source_context(file_path: str | Path) -> tuple[str | None, dict[str, Path]]:
    path = Path(file_path)
    if not path.is_absolute() or not path.is_file():
        return None, {}
    info = _package_info(path, Path(path.anchor))
    if info is None:
        identity = _file_identity(path)
        return identity, {identity: path}
    directory, _ = info
    root = _context_root(directory)
    return _module_path(path, root), _module_map(path, root)
