#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_installer import file_digest, validate_installer
from tools.identity import IDENTITY, project_version


def build_site(
    root: Path, assets: Path, output: Path, tag: str, *, skip_runtime_validation: bool = False
) -> None:
    version = project_version(root)
    if tag != f"v{version}":
        raise ValueError(f"site tag must be v{version}")
    archive_name = IDENTITY.archive_name(version)
    expected_assets = set(IDENTITY.release_assets(version))
    observed_assets = {path.name for path in assets.iterdir() if path.is_file()}
    if observed_assets != expected_assets:
        raise ValueError(
            f"release assets differ; expected={sorted(expected_assets)}, got={sorted(observed_assets)}"
        )
    validation_command = [
        "python",
        str(root / "tools/validate_release.py"),
        str(assets / archive_name),
        "--project-root",
        str(root),
        "--checksum",
        str(assets / f"{archive_name}.sha256"),
        "--sums",
        str(assets / "SHA256SUMS"),
    ]
    if skip_runtime_validation:
        validation_command.append("--skip-runtime")
    subprocess.run(
        validation_command,
        cwd=root,
        check=True,
    )
    archive_digest = file_digest(assets / archive_name)
    installer_path = assets / "install"
    installer = installer_path.read_bytes()
    text = installer.decode("utf-8")
    validate_installer(text, version, archive_digest)
    release_base = (
        f'readonly RELEASE_BASE="${{AI_WORKSTATION_RELEASE_BASE:-{IDENTITY.release_base_url}}}"'
    )
    if text.count(release_base) != 1:
        raise ValueError("installer release base is not the intended GitHub Release endpoint")
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise ValueError("site output must be a real directory")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copyfile(installer_path, output / "install", follow_symlinks=False)
    entries = tuple(output.rglob("*"))
    if entries != (output / "install",) or (output / "install").is_symlink():
        raise ValueError("Pages distribution must contain only a regular install file")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Build the {IDENTITY.command_name} GitHub Pages distribution tree"
    )
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--site-dir", required=True, type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--skip-runtime-validation", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        root = args.project_root.resolve()
        build_site(
            root,
            args.release_dir.resolve(),
            args.site_dir.resolve(),
            f"v{project_version(root)}",
            skip_runtime_validation=args.skip_runtime_validation,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"site build failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
