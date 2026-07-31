from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ai_setup.errors import CommandError


def manual_authentication_env() -> dict[str, str]:
    """Keep authentication URLs in the terminal instead of opening a browser."""
    terminal_browser = "/usr/bin/echo"
    return {
        "BROWSER": terminal_browser,
        "GH_BROWSER": terminal_browser,
    }


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    replace_env: bool = False
    sensitive_values: tuple[str, ...] = ()
    mutate: bool = True
    failure_component: str = "command"
    failure_operation: str = "execute"
    failure_packages: tuple[str, ...] = ()
    log_path: Path | None = None
    interactive: bool = False
    sensitive_output: bool = False


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    DIAGNOSTIC_PATTERNS = (
        " are in conflict",
        "conflicting files",
        "conflicting dependencies",
        "exists in filesystem",
        "failed to prepare transaction",
        "failed to commit transaction",
        "error:",
    )

    def __init__(
        self,
        *,
        dry_run: bool = False,
        verbose: bool = False,
        output: Callable[[str], None] = print,
    ) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.output = output
        self.history: list[Command] = []

    @staticmethod
    def redact(value: str, secrets: Sequence[str]) -> str:
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    @classmethod
    def diagnostic(cls, stdout: str, stderr: str) -> str:
        lines = [
            cls.ANSI_ESCAPE.sub("", line).strip()
            for line in (*stdout.splitlines(), *stderr.splitlines())
            if line.strip()
        ]
        for pattern in cls.DIAGNOSTIC_PATTERNS:
            for line in lines:
                if pattern in line.lower():
                    return line.removeprefix(":: ")[:500]
        return lines[-1][:500] if lines else "command exited nonzero"

    def _write_failure_log(self, command: Command, result: CommandResult) -> None:
        if command.log_path is None or command.sensitive_output:
            return
        rendered = self.redact(shlex.join(command.argv), command.sensitive_values)
        content = (
            f"Command: {rendered}\n"
            f"Exit status: {result.returncode}\n\n"
            f"Standard output:\n{result.stdout}"
            f"\nStandard error:\n{result.stderr}"
        )
        from ai_setup.config.files import write_workspace_file

        write_workspace_file(command.log_path, content, 0o600)

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        if not command.argv or any("\0" in arg for arg in command.argv):
            raise CommandError("command", "validate", "invalid argument vector")
        self.history.append(command)
        if self.verbose and not command.sensitive_output:
            rendered = shlex.join(command.argv)
            self.output("$ " + self.redact(rendered, command.sensitive_values))
        if self.dry_run and command.mutate:
            return CommandResult(command.argv, 0, "", "")
        env = {} if command.replace_env else os.environ.copy()
        if command.env:
            env.update(command.env)
        try:
            if command.interactive:
                completed = subprocess.run(
                    command.argv,
                    cwd=command.cwd,
                    env=env,
                    text=True,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                    check=False,
                )
            else:
                completed = subprocess.run(
                    command.argv,
                    cwd=command.cwd,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
        except OSError as exc:
            if not check:
                raise
            raise CommandError(
                command.failure_component,
                command.failure_operation,
                f"could not start {command.argv[0]}: {exc.strerror or exc}",
            ) from exc
        result = CommandResult(
            command.argv,
            completed.returncode,
            "" if command.interactive else self.redact(completed.stdout, command.sensitive_values),
            "" if command.interactive else self.redact(completed.stderr, command.sensitive_values),
        )
        if self.verbose and not command.sensitive_output:
            if result.stdout:
                self.output(result.stdout.rstrip())
            if result.stderr:
                self.output(result.stderr.rstrip())
        if check and result.returncode:
            if not command.interactive:
                self._write_failure_log(command, result)
            raise CommandError(
                command.failure_component,
                command.failure_operation,
                (
                    f"interactive command exited with status {result.returncode}"
                    if command.interactive
                    else (
                        "sensitive command output was suppressed"
                        if command.sensitive_output
                        else self.diagnostic(result.stdout, result.stderr)
                    )
                ),
                result.returncode,
                str(command.log_path) if command.log_path and not command.interactive else None,
                command.failure_packages,
            )
        return result


def interactive_terminal_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
