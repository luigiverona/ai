from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_setup.identity import IDENTITY

SEMANTIC_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class VersionResolutionError(RuntimeError):
    pass


def _trusted_project_file(package_file: Path) -> Path | None:
    package_dir = package_file.resolve().parent
    source_dir = package_dir.parent
    if package_dir.name != IDENTITY.import_package or source_dir.name != "src":
        return None
    return source_dir.parent / "pyproject.toml"


def _project_version(path: Path) -> str:
    if not path.is_file():
        raise VersionResolutionError(f"trusted project metadata is missing: {path}")
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data["project"]
        name = project["name"]
        version = project["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise VersionResolutionError(f"trusted project metadata is invalid: {path}") from exc
    if name != IDENTITY.distribution_name:
        raise VersionResolutionError(
            f"trusted project name must be {IDENTITY.distribution_name}, got {name!r}"
        )
    if not isinstance(version, str) or not SEMANTIC_VERSION.fullmatch(version):
        raise VersionResolutionError("trusted project version must be semantic X.Y.Z")
    return version


def resolve_version(
    *,
    package_file: Path | None = None,
    metadata_version: Callable[[str], str] = importlib.metadata.version,
) -> str:
    trusted = _trusted_project_file(package_file or Path(__file__))
    if trusted is not None:
        return _project_version(trusted)
    try:
        version = metadata_version(IDENTITY.distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise VersionResolutionError(
            f"cannot resolve {IDENTITY.distribution_name} version"
        ) from exc
    if not SEMANTIC_VERSION.fullmatch(version):
        raise VersionResolutionError("installed distribution version must be semantic X.Y.Z")
    return version
