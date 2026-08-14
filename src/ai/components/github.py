from ..errors import AiError
from ..runtime import Runtime


def reconcile(runtime: Runtime) -> None:
    if runtime.run(["gh", "--version"], check=False).returncode:
        runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", "github-cli"])
    authenticated = runtime.run(["gh", "auth", "status", "--hostname", "github.com"], check=False).returncode == 0
    if not authenticated:
        if runtime.dry_run:
            runtime.changed("authenticated GitHub account")
        else:
            runtime.run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "ssh"],
                        capture=False, mutate=True)
            if runtime.run(["gh", "auth", "status", "--hostname", "github.com"], check=False).returncode:
                raise AiError("GitHub: authentication did not complete")
            runtime.changed("authenticated GitHub account")
    if authenticated or not runtime.dry_run:
        account = runtime.run(["gh", "api", "user", "--jq", ".login"], check=False)
        if account.returncode or not account.stdout.strip():
            raise AiError("GitHub: could not verify authenticated account")
    protocol = runtime.run(["gh", "config", "get", "git_protocol", "--host", "github.com"],
                           check=False).stdout.strip()
    if protocol != "ssh":
        runtime.run(["gh", "config", "set", "git_protocol", "ssh", "--host", "github.com"], mutate=True)
        if not runtime.dry_run:
            actual = runtime.run(["gh", "config", "get", "git_protocol", "--host", "github.com"]).stdout.strip()
            if actual != "ssh":
                raise AiError("GitHub: failed to configure SSH Git operations")
        runtime.changed("configured SSH Git operations")
