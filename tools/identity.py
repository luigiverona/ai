from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEMANTIC_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    display_name: str
    command_name: str
    distribution_name: str
    import_package: str
    repository_slug: str
    archive_stem: str
    install_root_relative: str
    launcher_relative: str
    version_token: str
    digest_token: str
    release_temp_prefix: str
    validation_temp_prefix: str

    @property
    def release_base_url(self) -> str:
        return f"https://github.com/{self.repository_slug}/releases/download"

    def archive_name(self, version: str) -> str:
        return f"{self.archive_stem}-{version}.tar.gz"

    def release_assets(self, version: str) -> frozenset[str]:
        archive = self.archive_name(version)
        return frozenset({"install", archive, "SHA256SUMS"})


IDENTITY = ReleaseIdentity(
    display_name="ai",
    command_name="ai",
    distribution_name="ai-workstation",
    import_package="ai_setup",
    repository_slug="luigiverona/ai",
    archive_stem="ai",
    install_root_relative=".local/share/ai",
    launcher_relative=".local/bin/ai",
    version_token="@AI_WORKSTATION_VERSION@",  # noqa: S106 - deterministic template marker
    digest_token="@AI_WORKSTATION_ARCHIVE_SHA256@",  # noqa: S106 - deterministic template marker
    release_temp_prefix=".ai-release.",
    validation_temp_prefix="ai-release-validation-",
)


def project_version(root: Path) -> str:
    try:
        data: dict[str, Any] = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        name = project["name"]
        version = project["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("invalid project metadata") from exc
    if name != IDENTITY.distribution_name:
        raise ValueError(f"project.name must be {IDENTITY.distribution_name}")
    if not isinstance(version, str) or not SEMANTIC_VERSION.fullmatch(version):
        raise ValueError("project.version must be a semantic X.Y.Z version")
    return version
