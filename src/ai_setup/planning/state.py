from __future__ import annotations

from pathlib import Path

from ai_setup.execution.runner import Command, CommandRunner
from ai_setup.identity import IDENTITY
from ai_setup.models import Package, Source
from ai_setup.packages.managers import (
    YayReadiness,
    flatpak_state_exists,
    probe_environment,
    yay_readiness,
)


class StateInspector:
    def __init__(self, runner: CommandRunner, home: Path) -> None:
        self.runner = runner
        self.home = home

    def package_installed(self, package: Package) -> bool:
        if package.source is Source.AUR and package.identifier == "yay-bin":
            return self.aur_helper_readiness().ready
        if package.source in {Source.PACMAN, Source.AUR}:
            try:
                return (
                    self.runner.run(
                        Command(("pacman", "-Q", package.identifier), mutate=False), check=False
                    ).returncode
                    == 0
                )
            except FileNotFoundError:
                return False
        if package.source is Source.FLATPAK:
            if not flatpak_state_exists(self.home):
                return False
            try:
                with probe_environment(self.home, preserve_data=True) as environment:
                    return (
                        self.runner.run(
                            Command(
                                ("flatpak", "info", "--user", package.identifier),
                                env=environment,
                                mutate=False,
                            ),
                            check=False,
                        ).returncode
                        == 0
                    )
            except FileNotFoundError:
                return False
        if package.source is Source.UPSTREAM and package.identifier == "codex":
            executable = IDENTITY.codex_shared_binary(self.home)
            if not executable.is_file():
                return False
            return (
                self.runner.run(
                    Command((str(executable), "--version"), mutate=False), check=False
                ).returncode
                == 0
            )
        return False

    def aur_helper_readiness(self) -> YayReadiness:
        return yay_readiness(self.runner, self.home)

    def pending(self, packages: tuple[Package, ...]) -> tuple[Package, ...]:
        return tuple(package for package in packages if not self.package_installed(package))
