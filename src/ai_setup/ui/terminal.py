from __future__ import annotations

from collections.abc import Callable

from ai_setup.identity import IDENTITY


class Terminal:
    def __init__(
        self, *, input_fn: Callable[[str], str] = input, output: Callable[[str], None] = print
    ) -> None:
        self.input = input_fn
        self.output = output
        self._has_section = False

    def section(self, title: str) -> None:
        if self._has_section:
            self.output("")
        self.output(title)
        self._has_section = True

    def confirm(
        self,
        prompt: str,
        *,
        default: bool = False,
        assume_yes: bool = False,
        destructive: bool = False,
    ) -> bool:
        if assume_yes and not destructive:
            return True
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            try:
                answer = self.input(prompt + suffix).strip().lower()
            except EOFError:
                self.output("Input ended; confirmation declined.")
                return False
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.output("Please answer yes or no.")

    def error(
        self,
        component: str,
        operation: str,
        reason: str,
        log_path: str | None = None,
        packages: tuple[str, ...] = (),
    ) -> None:
        if packages:
            self.output(f"{component} failed.")
            self.output(f"Packages: {', '.join(packages)}.")
            self.output(f"Reason: {reason.rstrip('.')}.")
            if log_path:
                self.output(f"Details: {log_path}.")
            self.output(f"Run {IDENTITY.command_name} --verbose for complete command output.")
            return
        self.output(f"{component} failed while trying to {operation}: {reason.rstrip('.')}.")
        if log_path:
            self.output(f"Details: {log_path}.")
        self.output(f"Run {IDENTITY.command_name} --verbose for complete command output.")
