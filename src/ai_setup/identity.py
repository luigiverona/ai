from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    display_name: str
    command_name: str
    distribution_name: str
    import_package: str
    repository_slug: str
    installer_endpoint: str
    install_root_relative: PurePosixPath
    launcher_relative: PurePosixPath
    temporary_prefix: str
    ssh_key_filename: str
    ssh_fragment_filename: str
    shell_managed_marker: str
    fish_configuration_filename: str
    catalog_environment_variable: str

    def install_root(self, home: Path) -> Path:
        return home / self.install_root_relative

    def launcher(self, home: Path) -> Path:
        return home / self.launcher_relative

    def codex_state_root(self, home: Path) -> Path:
        return self.install_root(home) / "codex"

    def codex_shared_binary(self, home: Path) -> Path:
        return self.install_root(home) / "bin/codex"

    def ssh_key(self, home: Path) -> Path:
        return home / ".ssh" / self.ssh_key_filename

    def ssh_fragment(self, home: Path) -> Path:
        return home / ".ssh/config.d" / self.ssh_fragment_filename

    def fish_configuration(self, home: Path) -> Path:
        return home / ".config/fish/conf.d" / self.fish_configuration_filename


IDENTITY = ProjectIdentity(
    display_name="ai",
    command_name="ai",
    distribution_name="ai-workstation",
    import_package="ai_setup",
    repository_slug="luigiverona/ai",
    installer_endpoint="https://ai.luigiverona.dev/install",
    install_root_relative=PurePosixPath(".local/share/ai"),
    launcher_relative=PurePosixPath(".local/bin/ai"),
    temporary_prefix="ai-",
    ssh_key_filename="id_ed25519_ai_github",
    ssh_fragment_filename="ai-github.conf",
    shell_managed_marker="# Added by ai",
    fish_configuration_filename="ai.fish",
    catalog_environment_variable="AI_WORKSTATION_CATALOG_ROOT",
)
