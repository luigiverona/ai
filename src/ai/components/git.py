import sys

from ..errors import AiError
from ..runtime import Runtime

IDENTITY_KEYS = ("user.name", "user.email")


def _github_identity(runtime: Runtime) -> dict[str, str]:
    if not runtime.command_exists("gh"):
        return {}
    if runtime.run(["gh", "auth", "status", "--hostname", "github.com"],
                   check=False).returncode:
        return {}
    result = runtime.run(
        ["gh", "api", "user", "--jq", '[.name // "", .email // ""] | @tsv'],
        check=False)
    if result.returncode:
        return {}
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) != 2:
        return {}
    return dict(zip(IDENTITY_KEYS, fields))


def _request_identity(key: str) -> str:
    if not sys.stdin.isatty():
        raise AiError(f"Git: {key} is missing and could not be derived; run interactively")
    value = input(f"Git {key}: ").strip()
    if not value:
        raise AiError(f"Git: {key} cannot be empty")
    return value


def reconcile(runtime: Runtime) -> None:
    if runtime.run(["git", "--version"], check=False).returncode:
        runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", "git"])
    missing = []
    for key in IDENTITY_KEYS:
        current = runtime.run(["git", "config", "--global", "--get", key], check=False).stdout.strip()
        if not current:
            missing.append(key)
    derived = {} if runtime.dry_run or not missing else _github_identity(runtime)
    for key in missing:
        if runtime.dry_run:
            runtime.changed(f"configure {key}")
            continue
        wanted = derived.get(key) or _request_identity(key)
        runtime.run(["git", "config", "--global", key, wanted], mutate=True)
        actual = runtime.run(["git", "config", "--global", "--get", key]).stdout.strip()
        if actual != wanted:
            raise AiError(f"Git: failed to configure {key}")
        runtime.changed(f"configured {key}")
    key = "init.defaultBranch"
    wanted = "main"
    current = runtime.run(["git", "config", "--global", "--get", key], check=False).stdout.strip()
    if current != wanted:
        runtime.run(["git", "config", "--global", key, wanted], mutate=True)
        if not runtime.dry_run:
            actual = runtime.run(["git", "config", "--global", "--get", key]).stdout.strip()
            if actual != wanted:
                raise AiError(f"Git: failed to configure {key}")
        runtime.changed(f"configured {key}")
