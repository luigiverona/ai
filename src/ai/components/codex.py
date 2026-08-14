from __future__ import annotations

import os
import re
import json
import shutil
import tempfile
from pathlib import Path

from ..errors import AiError
from ..runtime import Runtime, ensure_safe_parent, reject_unsafe_existing


def _auth_cache_valid(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if data.get("auth_mode") == "chatgpt":
        tokens = data.get("tokens")
        return isinstance(tokens, dict) and all(tokens.get(key) for key in (
            "access_token", "account_id", "refresh_token"))
    return bool(data.get("OPENAI_API_KEY"))


def _profile(runtime: Runtime, number: str, binary: Path) -> None:
    home = runtime.home / ".local" / "share" / "ai" / "codex" / number
    launcher = runtime.home / ".local" / "bin" / f"codex-{number}"
    ensure_safe_parent(home, runtime.home)
    reject_unsafe_existing(launcher)
    script = (f'#!/bin/sh\nexport CODEX_HOME="{home}"\n'
              f'exec "{binary}" -c \'cli_auth_credentials_store="file"\' "$@"\n')
    legacy = f'#!/bin/sh\nexport CODEX_HOME="{home}"\nexec "{binary}" "$@"\n'
    current = launcher.read_text() if launcher.exists() and not launcher.is_symlink() else ""
    config = home / "config.toml"
    legacy_isolated = current == legacy and config.is_file() and \
        'cli_auth_credentials_store = "file"' in config.read_text().splitlines()
    if current != script and not legacy_isolated:
        runtime.atomic_write(launcher, script, 0o755)
        runtime.changed(f"configured profile {number}")
    if runtime.dry_run:
        return
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth = home / "auth.json"
    reject_unsafe_existing(auth)
    authenticated = _auth_cache_valid(auth)
    if not authenticated:
        marker = home / ".authentication-in-progress"
        runtime.atomic_write(marker, "Authentication may be safely resumed.\n", 0o600)
        try:
            runtime.run([str(launcher), "login"], capture=False, mutate=True)
            if not _auth_cache_valid(auth):
                raise AiError(f"Codex: profile {number} authentication did not complete")
        finally:
            marker.unlink(missing_ok=True)
        runtime.changed(f"authenticated profile {number}")


def reconcile(runtime: Runtime) -> None:
    binary = runtime.home / ".local" / "share" / "ai" / "bin" / "codex"
    ensure_safe_parent(binary.parent, runtime.home)
    reject_unsafe_existing(binary, allow_symlink=True)
    current_result = runtime.run([str(binary), "--version"], check=False) if binary.exists() else None
    valid = current_result is not None and current_result.returncode == 0
    current_match = re.search(r"(\d+\.\d+\.\d+)", current_result.stdout) if valid else None
    npm_available = runtime.run(["npm", "--version"], check=False).returncode == 0
    latest_result = None
    if npm_available:
        probe = Path(tempfile.mkdtemp(prefix="ai-codex-probe-"))
        try:
            latest_result = runtime.run(["npm", "view", "@openai/codex", "version"], check=False,
                                        env={"npm_config_cache": str(probe / "cache"),
                                             "npm_config_update_notifier": "false"})
        finally:
            shutil.rmtree(probe)
    latest = latest_result.stdout.strip() if latest_result and latest_result.returncode == 0 else None
    needs_install = not valid or (latest is not None and (not current_match or current_match.group(1) != latest))
    if needs_install:
        if runtime.dry_run:
            runtime.changed("installed Codex")
        else:
            if not npm_available:
                runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", "npm"])
            wanted = latest or "latest"
            temporary = Path(tempfile.mkdtemp(prefix="ai-codex-"))
            try:
                prefix = temporary / "install"
                runtime.run(["npm", "install", "--prefix", str(prefix), f"@openai/codex@{wanted}"], mutate=True)
                installed = prefix / "node_modules" / ".bin" / "codex"
                installed_version = runtime.run([str(installed), "--version"], check=False)
                if installed_version.returncode:
                    raise AiError("Codex: official package did not produce a working binary")
                match = re.search(r"(\d+\.\d+\.\d+)", installed_version.stdout)
                if not match:
                    raise AiError("Codex: could not identify installed version")
                root = binary.parent.parent
                active = root / f"codex-install-{match.group(1)}"
                staged = root / f".codex-install-{match.group(1)}.new"
                binary.parent.mkdir(parents=True, exist_ok=True)
                if staged.exists() or staged.is_symlink():
                    if staged.is_dir() and not staged.is_symlink():
                        shutil.rmtree(staged)
                    else:
                        raise AiError(f"Codex: unsafe staging collision: {staged}")
                shutil.copytree(prefix, staged, symlinks=True)
                if active.exists() or active.is_symlink():
                    if (active.is_dir() and not active.is_symlink() and active.stat().st_uid == os.getuid() and
                            runtime.run([str(active / "node_modules/.bin/codex"), "--version"],
                                        check=False).stdout == installed_version.stdout):
                        shutil.rmtree(staged)
                    else:
                        raise AiError(f"Codex: unsafe installation collision: {active}")
                else:
                    os.replace(staged, active)
                target = binary.parent / ".codex.new"
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    raise AiError(f"Codex: unsafe activation collision: {target}")
                target.symlink_to(active / "node_modules" / ".bin" / "codex")
                os.replace(target, binary)
                if runtime.run([str(binary), "--version"], check=False).returncode:
                    raise AiError("Codex: installed binary verification failed")
                for old in root.glob("codex-install-*"):
                    if old != active and old.is_dir() and not old.is_symlink():
                        shutil.rmtree(old)
            finally:
                shutil.rmtree(temporary)
            runtime.changed("installed Codex")
    for number in ("01", "02"):
        _profile(runtime, number, binary)
