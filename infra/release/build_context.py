#!/usr/bin/env python3
"""Create isolated ACR build contexts from an explicit source allowlist."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ("app", "agent")
COMMON_FILES = ("pyproject.toml", "uv.lock")
PYTHON_TREES = {
    "app": ("packages/domain/edu_domain", "services/app/edu_app"),
    "agent": ("packages/domain/edu_domain", "services/foundry-agent/edu_agent"),
}
WEB_FILES = (
    "apps/web/package.json",
    "apps/web/package-lock.json",
    "apps/web/index.html",
    "apps/web/tsconfig.json",
    "apps/web/vite.config.ts",
    "apps/web/public/auth-redirect.html",
)
WEB_SOURCE = "apps/web/src"
BOOTSTRAP_SCRIPT = "infra/release/bootstrap_db.py"
APP_AGENT_FILES = (
    "services/foundry-agent/edu_agent/__init__.py",
    "services/foundry-agent/edu_agent/demo.py",
    "services/foundry-agent/edu_agent/english_demo.py",
    "services/foundry-agent/edu_agent/knowledge_demo.py",
)
DOCKERFILES = {
    "app": "infra/docker/app.Dockerfile",
    "agent": "infra/docker/agent.Dockerfile",
}
WEB_SUFFIXES = {
    ".css",
    ".html",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".png",
    ".svg",
    ".ts",
    ".tsx",
    ".webp",
    ".woff",
    ".woff2",
}
EXCLUDED_COMPONENTS = {
    ".cache",
    ".edu-ai",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
    "state",
    "test",
    "tests",
    "venv",
}
SENSITIVE_MARKERS = (
    "credential",
    "id_ed25519",
    "id_rsa",
    "private_key",
    "secret",
    "token",
)
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


class ContextError(ValueError):
    """Raised when a context source cannot be included safely."""


def _is_excluded(relative: PurePosixPath) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    if any(part in EXCLUDED_COMPONENTS for part in parts):
        return True

    name = relative.name.casefold()
    if (
        name == ".env"
        or name.startswith(".env.")
        or any(marker in name for marker in SENSITIVE_MARKERS)
        or relative.suffix.casefold() in SENSITIVE_SUFFIXES
    ):
        return True
    if name.startswith("test_") or ".test." in name or ".spec." in name or name.endswith("_test.py"):
        return True
    return False


def _assert_regular_file(path: Path, relative: PurePosixPath) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContextError(f"required context source is missing: {relative}") from exc
    if stat.S_ISLNK(mode):
        raise ContextError(f"symlink is not allowed in a build context: {relative}")
    if not stat.S_ISREG(mode):
        raise ContextError(f"non-regular file is not allowed in a build context: {relative}")


def _walk_files(root: Path, directory: str, suffixes: set[str]) -> list[tuple[str, Path]]:
    relative_root = PurePosixPath(directory)
    source_root = root.joinpath(*relative_root.parts)
    try:
        mode = source_root.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContextError(f"required source directory is missing: {directory}") from exc
    if stat.S_ISLNK(mode):
        raise ContextError(f"symlink is not allowed in a build context: {directory}")
    if not stat.S_ISDIR(mode):
        raise ContextError(f"source path is not a directory: {directory}")

    for parent in relative_root.parents:
        if parent == PurePosixPath("."):
            continue
        _assert_directory_not_symlink(root.joinpath(*parent.parts), parent)

    collected: list[tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(source_root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs = []
        for dirname in sorted(dirnames):
            candidate = current_path / dirname
            relative = PurePosixPath(candidate.relative_to(root).as_posix())
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ContextError(f"symlink is not allowed in a build context: {relative}")
            if not stat.S_ISDIR(mode):
                raise ContextError(f"non-directory found while walking source tree: {relative}")
            if not _is_excluded(relative):
                safe_dirs.append(dirname)
        dirnames[:] = safe_dirs

        for filename in sorted(filenames):
            candidate = current_path / filename
            relative = PurePosixPath(candidate.relative_to(root).as_posix())
            _assert_regular_file(candidate, relative)
            if _is_excluded(relative) or candidate.suffix.casefold() not in suffixes:
                continue
            collected.append((relative.as_posix(), candidate))
    return collected


def _assert_directory_not_symlink(path: Path, relative: PurePosixPath) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContextError(f"required source directory is missing: {relative}") from exc
    if stat.S_ISLNK(mode):
        raise ContextError(f"symlink is not allowed in a build context: {relative}")
    if not stat.S_ISDIR(mode):
        raise ContextError(f"source path is not a directory: {relative}")


def _add_file(root: Path, files: dict[str, Path], source: str, archive_name: str | None = None) -> None:
    relative = PurePosixPath(source)
    if _is_excluded(relative):
        return
    path = root.joinpath(*relative.parts)
    for parent in relative.parents:
        if parent != PurePosixPath("."):
            _assert_directory_not_symlink(root.joinpath(*parent.parts), parent)
    _assert_regular_file(path, relative)
    name = archive_name or relative.as_posix()
    if name in files:
        raise ContextError(f"duplicate archive path: {name}")
    files[name] = path


def collect_context(root: Path, target: str) -> dict[str, Path]:
    if target not in TARGETS:
        raise ContextError(f"unknown context target: {target}")

    files: dict[str, Path] = {}
    for source in COMMON_FILES:
        _add_file(root, files, source)

    for source_dir in PYTHON_TREES[target]:
        for name, path in _walk_files(root, source_dir, {".py"}):
            if name in files:
                raise ContextError(f"duplicate archive path: {name}")
            files[name] = path

    if target == "app":
        for source in (*WEB_FILES, *APP_AGENT_FILES):
            _add_file(root, files, source)
        for name, path in _walk_files(root, WEB_SOURCE, WEB_SUFFIXES):
            if name in files:
                raise ContextError(f"duplicate archive path: {name}")
            files[name] = path
        _add_file(root, files, BOOTSTRAP_SCRIPT)

    _add_file(root, files, DOCKERFILES[target], archive_name="Dockerfile")
    return dict(sorted(files.items()))


def _verify_manifest(target: str, files: dict[str, Path]) -> None:
    names = set(files)
    required = {
        "Dockerfile",
        *COMMON_FILES,
        "packages/domain/edu_domain/__init__.py",
    }
    if target == "app":
        required.update(
            {
                "services/app/edu_app/__main__.py",
                "services/app/edu_app/api.py",
                "apps/web/package.json",
                "apps/web/package-lock.json",
                "apps/web/index.html",
                "apps/web/public/auth-redirect.html",
                BOOTSTRAP_SCRIPT,
                *APP_AGENT_FILES,
            }
        )
    else:
        required.update(
            {
                "services/foundry-agent/edu_agent/host.py",
                "services/foundry-agent/edu_agent/service.py",
            }
        )
    missing = sorted(required - names)
    if missing:
        raise ContextError(f"{target} context is missing required sources: {', '.join(missing)}")

    forbidden_roots = ("tests/", "apps/web/tests/", "apps/web/node_modules/", "apps/web/dist/")
    for name in names:
        path = PurePosixPath(name)
        if _is_excluded(path) or name.startswith(forbidden_roots):
            raise ContextError(f"excluded path entered {target} context: {name}")


def _read_nofollow(source: Path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise ContextError(f"source changed while creating context: {source}")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _read_nofollow(source) as source_file, destination.open("xb") as destination_file:
        shutil.copyfileobj(source_file, destination_file)
    destination.chmod(0o644)


def _output_is_safe(root: Path, output_dir: Path) -> Path:
    destination = output_dir.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ContextError("output directory must be outside the repository")
    return destination


def create_contexts(root: Path, output_dir: Path, targets: tuple[str, ...]) -> None:
    destination = _output_is_safe(root, output_dir)
    manifests = {target: collect_context(root, target) for target in targets}
    for target, files in manifests.items():
        _verify_manifest(target, files)
        if (destination / target).exists():
            raise ContextError(f"output already exists for {target}: {destination}")

    destination.mkdir(parents=True, exist_ok=True)
    for target, files in manifests.items():
        context_dir = destination / target
        context_dir.mkdir()
        for name, source in files.items():
            _copy_file(source, context_dir.joinpath(*PurePosixPath(name).parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=(*TARGETS, "all"), default="all")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="destination outside the repository for the isolated context directories",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and print allowlisted context manifests without writing files",
    )
    args = parser.parse_args(argv)
    targets = TARGETS if args.target == "all" else (args.target,)

    try:
        manifests = {target: collect_context(ROOT, target) for target in targets}
        for target, files in manifests.items():
            _verify_manifest(target, files)
        if args.check:
            for target, files in manifests.items():
                print(f"{target}: {len(files)} allowlisted files")
                for name in files:
                    print(f"  {name}")
            return 0
        if args.output_dir is None:
            parser.error("--output-dir is required unless --check is used")
        create_contexts(ROOT, args.output_dir, targets)
    except (ContextError, OSError) as exc:
        print(f"build_context: {exc}", file=sys.stderr)
        return 2

    destination = _output_is_safe(ROOT, args.output_dir)
    for target in targets:
        print(f"{target}: {destination / target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
