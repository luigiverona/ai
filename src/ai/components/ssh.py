from __future__ import annotations

import os
import glob
import shlex
from pathlib import Path

from ..errors import AiError
from ..runtime import Runtime, ensure_safe_parent, reject_unsafe_existing

HOST_KEYS = """github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
"""


def _config(home: Path) -> str:
    key = str(home / ".ssh/id_ed25519_ai_github").replace('"', '\\"')
    known = str(home / ".ssh/known_hosts_ai_github").replace('"', '\\"')
    return f"""Host github.com
  HostName github.com
  User git
  IdentityFile "{key}"
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  UserKnownHostsFile "{known}"
"""


def _includes_fragment(text: str, ssh_dir: Path, fragment: Path) -> bool:
    for line in text.splitlines():
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            continue
        if not fields or fields[0].lower() != "include":
            continue
        for pattern in fields[1:]:
            if pattern.startswith("~/"):
                pattern = str(ssh_dir.parent / pattern[2:])
            elif not Path(pattern).is_absolute():
                pattern = str(ssh_dir / pattern)
            if any(Path(match) == fragment for match in glob.glob(pattern)):
                return True
    return False


def _fragment_correct(text: str, home: Path) -> bool:
    fields = []
    for line in text.splitlines():
        parsed = shlex.split(line, comments=True)
        if parsed:
            fields.append(tuple(parsed))
    expected = [
        ("Host", "github.com"), ("HostName", "github.com"), ("User", "git"),
        ("IdentityFile", str(home / ".ssh/id_ed25519_ai_github")),
        ("IdentitiesOnly", "yes"), ("StrictHostKeyChecking", "yes"),
        ("UserKnownHostsFile", str(home / ".ssh/known_hosts_ai_github")),
    ]
    return [(key.lower(), *values) for key, *values in fields] == [
        (key.lower(), *values) for key, *values in expected]


def reconcile(runtime: Runtime) -> None:
    missing = [command for command in ("ssh", "ssh-keygen", "gh")
               if not runtime.command_exists(command)]
    packages = [package for package, commands in (("openssh", {"ssh", "ssh-keygen"}),
                                                   ("github-cli", {"gh"}))
                if commands.intersection(missing)]
    if packages:
        runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", *packages])
    ssh_dir = runtime.home / ".ssh"
    ensure_safe_parent(ssh_dir, runtime.home)
    key = ssh_dir / "id_ed25519_ai_github"
    public = key.with_suffix(".pub")
    fragment = ssh_dir / "config.d" / "ai-github.conf"
    known = ssh_dir / "known_hosts_ai_github"
    for path in (key, public, fragment, known):
        reject_unsafe_existing(path)
    ensure_safe_parent(fragment.parent, runtime.home)
    if not key.exists():
        if public.exists():
            raise AiError(f"SSH: public key exists without private key: {public}")
        if not runtime.dry_run:
            ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            runtime.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ai-github",
                         "-f", str(key)], mutate=True)
        runtime.changed("created GitHub SSH identity")
    elif not runtime.dry_run:
        generated = " ".join(runtime.run(
            ["ssh-keygen", "-y", "-f", str(key)]).stdout.split()[:2])
        actual = " ".join(public.read_text().split()[:2]) if public.exists() else ""
        if actual != generated:
            runtime.atomic_write(public, generated + " ai-github\n", 0o644)
            runtime.changed("repaired GitHub SSH public key")
    wanted = _config(runtime.home)
    if not fragment.exists() or not _fragment_correct(fragment.read_text(), runtime.home):
        runtime.atomic_write(fragment, wanted, 0o600)
        runtime.changed("configured GitHub SSH identity")
    if not known.exists() or known.read_text() != HOST_KEYS:
        runtime.atomic_write(known, HOST_KEYS, 0o600)
        runtime.changed("pinned GitHub host key")
    main_config = ssh_dir / "config"
    include = "Include config.d/*.conf\n"
    if main_config.exists():
        reject_unsafe_existing(main_config)
        text = main_config.read_text()
        if not _includes_fragment(text, ssh_dir, fragment):
            raise AiError("SSH: ~/.ssh/config exists but does not include config.d/*.conf; add it explicitly")
    else:
        runtime.atomic_write(main_config, include, 0o600)
        runtime.changed("enabled SSH configuration fragments")
    modes = ((ssh_dir, 0o700), (fragment.parent, 0o700), (key, 0o600),
             (public, 0o644), (fragment, 0o600), (known, 0o600), (main_config, 0o600))
    repaired_mode = False
    for path, mode in modes:
        if path.exists() and (path.stat().st_mode & 0o777) != mode:
            if not runtime.dry_run:
                os.chmod(path, mode)
            repaired_mode = True
    if repaired_mode:
        runtime.changed("repaired SSH permissions")
    if runtime.dry_run:
        return
    effective = runtime.run(["ssh", "-G", "github.com"]).stdout.lower()
    required = ["identitiesonly yes", "stricthostkeychecking true", str(key).lower(), str(known).lower()]
    if any(value not in effective for value in required):
        raise AiError("SSH: effective GitHub SSH configuration is not strict")
    pubtext = public.read_text().strip()
    listed_result = runtime.run(["gh", "api", "user/keys", "--paginate", "--jq", ".[].key"], check=False)
    if listed_result.returncode:
        raise AiError("SSH: could not list GitHub SSH keys")
    try:
        listed = {" ".join(line.split()[:2]) for line in listed_result.stdout.splitlines() if line.strip()}
    except TypeError as exc:
        raise AiError("SSH: invalid GitHub SSH key list response") from exc
    material = " ".join(pubtext.split()[:2])
    if material not in listed:
        runtime.run(["gh", "ssh-key", "add", str(public), "--title", "ai workstation"], mutate=True)
        runtime.changed("registered key with GitHub")
    result = runtime.run(["ssh", "-T", "git@github.com"], check=False)
    combined = result.stdout + result.stderr
    if result.returncode not in (0, 1) or "successfully authenticated" not in combined:
        raise AiError("SSH: GitHub connectivity verification failed")
