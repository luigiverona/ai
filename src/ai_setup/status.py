from __future__ import annotations

from pathlib import Path

from ai_setup.config.shell import ShellInfo
from ai_setup.execution.runner import Command, CommandResult, CommandRunner
from ai_setup.identity import IDENTITY
from ai_setup.models import Catalog, RunOptions
from ai_setup.ui.terminal import Terminal
from ai_setup.verification import readiness


class ReadOnlyRunner(CommandRunner):
    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        if command.mutate:
            raise RuntimeError(f"status refused mutating command: {command.argv[0]}")
        return super().run(command, check=check)


class StatusWorkflow:
    def __init__(
        self,
        catalog: Catalog,
        options: RunOptions,
        terminal: Terminal,
        *,
        runner: CommandRunner | None = None,
        target_shell: ShellInfo | None = None,
        system_release: Path = Path("/etc/os-release"),
    ) -> None:
        self.scope = readiness.ReadinessScope.complete(catalog)
        self.options = options
        self.terminal = terminal
        self.runner = runner or ReadOnlyRunner(verbose=options.verbose, output=terminal.output)
        self.target_shell = target_shell
        self.system_release = system_release

    def run(self) -> int:
        verifier = readiness.ReadinessVerifier(
            self.scope,
            self.runner,
            self.options.home,
            target_shell=self.target_shell,
            system_release=self.system_release,
        )
        self.terminal.section("Status")
        try:
            results = verifier.results(read_only=True)
        except KeyboardInterrupt:
            self.terminal.output("Status check paused.")
            self.terminal.output(f"Run {IDENTITY.command_name} status again to continue.")
            return 130
        failures = [result for result in results if not result.passed]
        if failures:
            for result in failures:
                self.terminal.output(f"{result.name}: {result.reason}.")
            self.terminal.output("")
            self.terminal.output("Workstation is not ready.")
            return 1
        readiness.render_readiness(self.scope, self.terminal)
        self.terminal.output("")
        self.terminal.output("Workstation ready.")
        return 0
