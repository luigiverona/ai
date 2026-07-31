from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ai_setup.config.codex import CODEX_PROFILES, CodexManager
from ai_setup.config.git import GitConfigurator
from ai_setup.config.github import GitHubConfigurator
from ai_setup.config.shell import ShellInfo, detect_shell
from ai_setup.config.ssh import SSHManager
from ai_setup.execution.runner import CommandRunner
from ai_setup.models import Capability, Catalog, Package, Plan
from ai_setup.planning.state import StateInspector
from ai_setup.ui.terminal import Terminal
from ai_setup.verification.checks import CheckResult, Verifier


@dataclass(frozen=True, slots=True)
class ReadinessScope:
    capabilities: frozenset[Capability]
    packages: tuple[Package, ...]

    @classmethod
    def from_plan(cls, plan: Plan) -> ReadinessScope:
        return cls(frozenset((*plan.selected, *plan.prerequisites)), plan.packages)

    @classmethod
    def complete(cls, catalog: Catalog) -> ReadinessScope:
        packages = {
            (package.source, package.identifier): package
            for package in (*catalog.apps, *catalog.deps)
        }
        ordered = tuple(
            sorted(
                packages.values(), key=lambda package: (package.source.value, package.identifier)
            )
        )
        return cls(frozenset(Capability), ordered)


class ReadinessVerifier:
    def __init__(
        self,
        scope: ReadinessScope,
        runner: CommandRunner,
        home: Path,
        *,
        target_shell: ShellInfo | None = None,
        system_release: Path = Path("/etc/os-release"),
        inspector: StateInspector | None = None,
    ) -> None:
        self.scope = scope
        self.runner = runner
        self.home = home
        self.target_shell = target_shell
        self.system_release = system_release
        self.inspector = inspector or StateInspector(runner, home)

    @staticmethod
    def _availability_check(
        name: str,
        operation: Callable[[], bool],
        reason: str,
        unavailable: str,
        *,
        tolerate_missing: bool,
    ) -> CheckResult:
        try:
            passed = operation()
        except FileNotFoundError:
            if not tolerate_missing:
                raise
            return CheckResult(name, False, unavailable)
        return CheckResult(name, passed, reason)

    def _shell(self) -> ShellInfo:
        if self.target_shell is None:
            self.target_shell = detect_shell()
        return self.target_shell

    def results(self, *, read_only: bool = False) -> list[CheckResult]:
        verifier = Verifier(self.runner, self.home, inspector=self.inspector)
        capabilities = self.scope.capabilities
        results = [
            verifier.system(self.system_release),
            *(verifier.package(package) for package in self.scope.packages),
        ]
        if Capability.FLATHUB in capabilities:
            results.append(verifier.flathub())
        if Capability.GIT in capabilities:
            results.append(
                self._availability_check(
                    "Git identity",
                    GitConfigurator(self.runner).verify,
                    "identity or default branch missing",
                    "Git is not installed",
                    tolerate_missing=read_only,
                )
            )
        if Capability.GITHUB in capabilities:
            github = GitHubConfigurator(self.runner)
            results.append(
                self._availability_check(
                    "GitHub authentication",
                    github.authenticated,
                    "not authenticated",
                    "GitHub CLI is not installed",
                    tolerate_missing=read_only,
                )
            )
            results.append(
                self._availability_check(
                    "GitHub SSH protocol",
                    lambda: github.protocol() == "ssh",
                    "protocol is not SSH",
                    "GitHub CLI is not installed",
                    tolerate_missing=read_only,
                )
            )
        if Capability.SSH in capabilities:
            results.append(
                self._availability_check(
                    "SSH connection",
                    lambda: SSHManager(self.runner, self.home).verify(read_only=read_only),
                    "connection failed",
                    "OpenSSH client is not installed",
                    tolerate_missing=read_only,
                )
            )
        if Capability.CODEX in capabilities:
            codex = CodexManager(self.runner, self.home)
            for profile in CODEX_PROFILES:
                results.append(
                    self._availability_check(
                        profile.display_label,
                        partial(codex.verified, profile.identifier),
                        "profile not authenticated",
                        "managed Codex executable is not available",
                        tolerate_missing=read_only,
                    )
                )
            results.append(
                CheckResult(
                    "Codex profile isolation",
                    codex.profiles_distinct(),
                    "launcher CODEX_HOME values are not distinct",
                )
            )
        if Capability.SHELL in capabilities:
            results.append(verifier.shell_configuration(self._shell()))
        return results


def render_readiness(scope: ReadinessScope, terminal: Terminal) -> None:
    capabilities = scope.capabilities
    if scope.packages:
        terminal.output("All software requirements are ready.")
    if Capability.GIT in capabilities:
        terminal.output("The Git identity is ready.")
    if Capability.GITHUB in capabilities:
        terminal.output("GitHub authentication is ready.")
    if Capability.SSH in capabilities:
        terminal.output("The SSH connection is ready.")
    if Capability.CODEX in capabilities:
        terminal.output("Both Codex profiles are ready.")
    if Capability.SHELL in capabilities:
        terminal.output("The shell PATH is ready.")
