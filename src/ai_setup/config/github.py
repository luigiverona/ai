from __future__ import annotations

from ai_setup.errors import ValidationError
from ai_setup.execution.runner import (
    Command,
    CommandRunner,
    interactive_terminal_available,
    manual_authentication_env,
)
from ai_setup.identity import IDENTITY

AUTH_STATUS_ARGV = ("gh", "auth", "status", "--hostname", "github.com")
AUTH_LOGIN_ARGV = (
    "gh",
    "auth",
    "login",
    "--hostname",
    "github.com",
    "--web",
    "--git-protocol",
    "ssh",
    "--skip-ssh-key",
)
PROTOCOL_GET_ARGV = ("gh", "config", "get", "git_protocol", "--host", "github.com")
PROTOCOL_SET_ARGV = ("gh", "config", "set", "git_protocol", "ssh", "--host", "github.com")
_PROTOCOL_NOT_INSPECTED = object()


class GitHubConfigurator:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def authenticated(self) -> bool:
        return (
            self.runner.run(
                Command(AUTH_STATUS_ARGV, mutate=False),
                check=False,
            ).returncode
            == 0
        )

    def authenticate(
        self,
        *,
        authenticated: bool | None = None,
        protocol: str | None | object = _PROTOCOL_NOT_INSPECTED,
    ) -> None:
        signed_in = self.authenticated() if authenticated is None else authenticated
        login_performed = False
        if not signed_in:
            if not self.runner.dry_run and not interactive_terminal_available():
                raise ValidationError(
                    "GitHub",
                    "authenticate",
                    "authentication requires an interactive terminal; "
                    f"run {IDENTITY.command_name} github from a terminal",
                )
            self.runner.run(
                Command(
                    AUTH_LOGIN_ARGV,
                    env=manual_authentication_env(),
                    interactive=True,
                    failure_component="GitHub",
                    failure_operation="authenticate",
                )
            )
            login_performed = True
            if not self.runner.dry_run and not self.authenticated():
                raise ValidationError(
                    "GitHub",
                    "verify authentication",
                    "sign-in was cancelled or did not complete",
                )
        configured_protocol = (
            self.protocol() if login_performed or protocol is _PROTOCOL_NOT_INSPECTED else protocol
        )
        if configured_protocol != "ssh":
            self.runner.run(Command(PROTOCOL_SET_ARGV))
            if not self.runner.dry_run and self.protocol() != "ssh":
                raise ValidationError(
                    "GitHub",
                    "verify Git protocol",
                    "Git protocol is not configured for SSH",
                )

    def protocol(self) -> str | None:
        result = self.runner.run(
            Command(PROTOCOL_GET_ARGV, mutate=False),
            check=False,
        )
        return result.stdout.strip() or None

    def account(self) -> str | None:
        result = self.runner.run(
            Command(("gh", "api", "user", "--jq", ".login"), mutate=False), check=False
        )
        return result.stdout.strip() or None
