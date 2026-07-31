from __future__ import annotations

from pathlib import Path

from ai_setup.execution.runner import Command, CommandResult


class FakeRunner:
    def __init__(
        self, responses: dict[tuple[str, ...], CommandResult] | None = None, dry_run: bool = False
    ) -> None:
        self.responses = responses or {}
        self.dry_run = dry_run
        self.commands: list[Command] = []

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        self.commands.append(command)
        return self.responses.get(command.argv, CommandResult(command.argv, 0, "", ""))


def write_manifest(root: Path, kind: str, category: str, content: str) -> None:
    path = root / kind / category / "manifest.toml"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
