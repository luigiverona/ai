from __future__ import annotations

from dataclasses import dataclass

from ai_setup.execution.runner import Command, CommandRunner


@dataclass(frozen=True, slots=True)
class GitIdentity:
    name: str
    email: str


class GitConfigurator:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def get(self, key: str) -> str | None:
        result = self.runner.run(
            Command(("git", "config", "--global", "--get", key), mutate=False), check=False
        )
        return result.stdout.strip() or None

    def configure(self, identity: GitIdentity) -> None:
        for key, value in (
            ("user.name", identity.name),
            ("user.email", identity.email),
            ("init.defaultBranch", "main"),
        ):
            if self.get(key) != value:
                self.runner.run(Command(("git", "config", "--global", key, value)))

    def verify(self) -> bool:
        return bool(
            self.get("user.name")
            and self.get("user.email")
            and self.get("init.defaultBranch") == "main"
        )
