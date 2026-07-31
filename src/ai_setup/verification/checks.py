from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from ai_setup.config.shell import ShellInfo, path_configured
from ai_setup.execution.runner import CommandRunner
from ai_setup.models import Package, Source
from ai_setup.packages.managers import flathub_readiness
from ai_setup.planning.state import StateInspector


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    reason: str = ""


class Verifier:
    def __init__(
        self,
        runner: CommandRunner,
        home: Path,
        *,
        inspector: StateInspector | None = None,
    ) -> None:
        self.runner = runner
        self.home = home
        self.inspector = inspector or StateInspector(runner, home)

    def system(self, os_release_path: Path = Path("/etc/os-release")) -> CheckResult:
        os_release = os_release_path.read_text(encoding="utf-8")
        good = "ID=arch" in os_release and platform.machine() == "x86_64"
        return CheckResult("supported system", good, "Arch Linux x86_64 required")

    def package(self, package: Package) -> CheckResult:
        if package.source is Source.AUR and package.identifier == "yay-bin":
            readiness = self.inspector.aur_helper_readiness()
            if readiness.dependency_satisfied and not readiness.executable_runnable:
                return CheckResult(package.name, False, "installed AUR helper is not runnable")
            return CheckResult(package.name, readiness.ready, "not installed")
        installed = self.inspector.package_installed(package)
        return CheckResult(package.name, installed, "not installed")

    def shell_configuration(self, shell: ShellInfo) -> CheckResult:
        return CheckResult(
            "shell PATH configuration",
            path_configured(self.home, shell),
            f"~/.local/bin is not configured for {shell.name}",
        )

    def flathub(self) -> CheckResult:
        readiness = flathub_readiness(self.runner, self.home)
        if not readiness.flatpak_available:
            return CheckResult("Flathub remote", False, "Flatpak is not installed")
        return CheckResult("Flathub remote", readiness.configured, "missing")
