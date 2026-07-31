#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_installer import build_installer, validate_template
from tools.identity import IDENTITY, project_version

REQUIRED_FILES = {"LICENSE", "README.md", "pyproject.toml"}


def validate_release_notes(root: Path, version: str) -> None:
    path = root / "docs/releases" / f"v{version}.md"
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"release notes are missing: docs/releases/v{version}.md")
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"release notes are empty: docs/releases/v{version}.md")
    expected_heading = f"# {IDENTITY.display_name} {version}"
    headings = re.findall(r"(?m)^# .+$", content)
    if headings != [expected_heading]:
        raise ValueError(f"release notes heading must be exactly: {expected_heading}")


def validate_workflow_versions(root: Path, version: str) -> None:
    ci = ".github/workflows/ci.yml"
    declaration = f"python tools/build_release.py --tag v{version} --check-only"
    content = (root / ci).read_text(encoding="utf-8")
    if content.count(declaration) != 1:
        raise ValueError(f"workflow version declaration differs: {ci}")
    for name in (
        ".github/workflows/release.yml",
        ".github/workflows/pages.yml",
        ".github/workflows/bootstrap-test.yml",
    ):
        content = (root / name).read_text(encoding="utf-8")
        if re.search(r"(?m)^\s+default:\s+v[0-9]", content):
            raise ValueError(f"workflow must not independently default a release version: {name}")


def validate_release_contract(root: Path, tag: str) -> str:
    version = project_version(root)
    if tag != f"v{version}":
        raise ValueError(f"release tag must be v{version}, got {tag}")
    validate_template((root / "bootstrap/install.in").read_text(encoding="utf-8"))
    validate_release_notes(root, version)
    validate_workflow_versions(root, version)
    return version


def ensure_clean(root: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        raise ValueError("release source tree is dirty")


def commit_epoch(root: Path) -> int:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    epoch = int(result.stdout.strip())
    if epoch < 1:
        raise ValueError("invalid release commit timestamp")
    return epoch


def tracked_files(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    paths = [Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw]
    return tuple(sorted(paths, key=lambda item: item.as_posix()))


def is_runtime_file(path: Path) -> bool:
    value = path.as_posix()
    if value in REQUIRED_FILES:
        return True
    if value.startswith(f"src/{IDENTITY.import_package}/"):
        return path.suffix == ".py"
    parts = path.parts
    return len(parts) == 3 and parts[0] in {"apps", "deps"} and parts[2] == "manifest.toml"


def runtime_files(root: Path) -> tuple[Path, ...]:
    selected = tuple(path for path in tracked_files(root) if is_runtime_file(path))
    selected_names = {path.as_posix() for path in selected}
    missing = REQUIRED_FILES - selected_names
    if missing:
        raise ValueError(f"required runtime files are not tracked: {', '.join(sorted(missing))}")
    cli_path = f"src/{IDENTITY.import_package}/cli.py"
    if not any(path.as_posix() == cli_path for path in selected):
        raise ValueError(f"{cli_path} is not tracked")
    if not any(path.parts[0] == "apps" for path in selected):
        raise ValueError("application manifests are missing")
    if not any(path.parts[0] == "deps" for path in selected):
        raise ValueError("dependency manifests are missing")
    for path in selected:
        source = root / path
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"runtime path must be a regular file: {path.as_posix()}")
    return selected


def archive_directories(files: tuple[Path, ...]) -> tuple[PurePosixPath, ...]:
    directories: set[PurePosixPath] = set()
    for path in files:
        parent = PurePosixPath(path.as_posix()).parent
        while str(parent) != ".":
            directories.add(parent)
            parent = parent.parent
    return tuple(sorted(directories, key=lambda item: item.as_posix()))


def tar_info(name: str, *, epoch: int, directory: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = 0o755 if directory else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    return info


def atomic_text(path: Path, content: str) -> None:
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build(root: Path, output: Path, tag: str, *, allow_dirty: bool = False) -> tuple[Path, str]:
    version = validate_release_contract(root, tag)
    if not allow_dirty:
        ensure_clean(root)
    epoch = commit_epoch(root)
    files = runtime_files(root)
    archive_root = f"{IDENTITY.archive_stem}-{version}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{archive_root}.tar.gz"
    installer = output / "install"
    expected_outputs = set(IDENTITY.release_assets(version))
    unexpected = {path.name for path in output.iterdir() if path.name not in expected_outputs}
    if unexpected:
        raise ValueError(f"release output contains unexpected files: {sorted(unexpected)}")
    tar_descriptor, tar_raw = tempfile.mkstemp(prefix=IDENTITY.release_temp_prefix, dir=output)
    os.close(tar_descriptor)
    tar_path = Path(tar_raw)
    gzip_descriptor, gzip_raw = tempfile.mkstemp(prefix=f".{archive.name}.", dir=output)
    os.close(gzip_descriptor)
    gzip_path = Path(gzip_raw)
    try:
        with tarfile.open(tar_path, "w", format=tarfile.USTAR_FORMAT) as bundle:
            bundle.addfile(tar_info(archive_root, epoch=epoch, directory=True))
            for directory in archive_directories(files):
                bundle.addfile(
                    tar_info(
                        f"{archive_root}/{directory.as_posix()}",
                        epoch=epoch,
                        directory=True,
                    )
                )
            for relative in files:
                data = (root / relative).read_bytes()
                info = tar_info(
                    f"{archive_root}/{relative.as_posix()}",
                    epoch=epoch,
                    directory=False,
                    size=len(data),
                )
                import io

                bundle.addfile(info, io.BytesIO(data))
        with tar_path.open("rb") as source, gzip_path.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=epoch
            ) as compressed:
                while chunk := source.read(1024 * 1024):
                    compressed.write(chunk)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.chmod(gzip_path, 0o644)
        os.replace(gzip_path, archive)
    finally:
        tar_path.unlink(missing_ok=True)
        gzip_path.unlink(missing_ok=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive_line = f"{digest}  {archive.name}\n"
    atomic_text(output / f"{archive.name}.sha256", archive_line)
    installer_digest = build_installer(root / "bootstrap/install.in", version, archive, installer)
    sums = archive_line + f"{installer_digest}  install\n"
    atomic_text(output / "SHA256SUMS", sums)
    observed = {path.name for path in output.iterdir() if path.is_file()}
    if observed != expected_outputs:
        raise ValueError(
            f"release output differs; expected={sorted(expected_outputs)}, got={sorted(observed)}"
        )
    return archive, digest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Build a reproducible {IDENTITY.command_name} runtime release"
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.source.resolve()
    try:
        version = validate_release_contract(root, args.tag)
        if args.check_only:
            print(f"release contract valid for v{version}")
            return 0
        output = (args.output or root / "dist").resolve()
        archive, digest = build(root, output, args.tag, allow_dirty=args.allow_dirty)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"release build failed: {exc}") from exc
    print(f"{digest}  {archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
