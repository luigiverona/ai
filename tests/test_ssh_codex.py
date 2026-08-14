import pytest
import json

from ai.components import codex, ssh
from ai.errors import AiError
from conftest import FakeRuntime


def test_ssh_rejects_collision(tmp_path):
    path = tmp_path / ".ssh/id_ed25519_ai_github"
    path.parent.mkdir()
    path.mkdir()
    with pytest.raises(AiError):
        ssh.reconcile(FakeRuntime(tmp_path))


def test_ssh_dry_run_is_non_mutating(tmp_path):
    responses = {("ssh", "--version"): (0, "", ""), ("ssh-keygen", "--version"): (0, "", ""),
                 ("gh", "--version"): (0, "", "")}
    runtime = FakeRuntime(tmp_path, responses, dry_run=True)
    ssh.reconcile(runtime)
    assert list(tmp_path.iterdir()) == []
    assert "created GitHub SSH identity" in runtime.changes


def test_ssh_present_commands_do_not_trigger_package_install(tmp_path):
    runtime = FakeRuntime(tmp_path, dry_run=True)
    ssh.reconcile(runtime)
    assert not any(args[:3] == ("sudo", "--", "pacman") for args, _, _ in runtime.calls)


def test_ssh_accepts_exact_tilde_include(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    fragment = ssh_dir / "config.d/ai-github.conf"
    fragment.parent.mkdir(parents=True)
    fragment.write_text("managed")
    assert ssh._includes_fragment("Include ~/.ssh/config.d/ai-github.conf\n", ssh_dir, fragment)


def test_codex_profiles_are_isolated_and_idempotent(tmp_path):
    binary = tmp_path / ".local/share/ai/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary")
    runtime = FakeRuntime(tmp_path, {(str(binary), "--version"): (0, "codex 1\n", "")}, dry_run=True)
    codex.reconcile(runtime)
    assert "configured profile 01" in runtime.changes
    assert "configured profile 02" in runtime.changes
    assert not (tmp_path / ".local/bin").exists()


def test_interrupted_authentication_marker_is_removed(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("x")
    launcher = tmp_path / ".local/bin/codex-01"
    auth = tmp_path / ".local/share/ai/codex/01/auth.json"
    def login(argv, cwd):
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "access", "account_id": "account", "refresh_token": "refresh"}}))
        return (0, "", "")
    runtime = FakeRuntime(tmp_path, {(str(launcher), "login"): login})
    codex._profile(runtime, "01", binary)
    assert auth.exists()
    assert not (auth.parent / ".authentication-in-progress").exists()


def test_ssh_existing_registered_key_is_not_duplicated(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    (ssh_dir / "config.d").mkdir(parents=True)
    key = ssh_dir / "id_ed25519_ai_github"
    public = key.with_suffix(".pub")
    key.write_text("private")
    public.write_text("ssh-ed25519 AAAATEST ai-github\n")
    (ssh_dir / "config.d/ai-github.conf").write_text(ssh._config(tmp_path))
    (ssh_dir / "known_hosts_ai_github").write_text(ssh.HOST_KEYS)
    (ssh_dir / "config").write_text("Include config.d/*.conf\n")
    ssh_dir.chmod(0o700)
    (ssh_dir / "config.d").chmod(0o700)
    key.chmod(0o600)
    for path in (ssh_dir / "config.d/ai-github.conf", ssh_dir / "known_hosts_ai_github",
                 ssh_dir / "config"):
        path.chmod(0o600)
    effective = "\n".join(["identitiesonly yes", "stricthostkeychecking true", str(key),
                             str(ssh_dir / "known_hosts_ai_github")])
    responses = {
        ("ssh", "--version"): (0, "", ""), ("ssh-keygen", "--version"): (0, "", ""),
        ("gh", "--version"): (0, "", ""),
        ("ssh-keygen", "-y", "-f", str(key)): (0, "ssh-ed25519 AAAATEST private-comment\n", ""),
        ("ssh", "-G", "github.com"): (0, effective, ""),
        ("gh", "api", "user/keys", "--paginate", "--jq", ".[].key"):
            (0, "ssh-ed25519 AAAATEST\n", ""),
        ("ssh", "-T", "git@github.com"): (1, "", "successfully authenticated"),
    }
    runtime = FakeRuntime(tmp_path, responses)
    ssh.reconcile(runtime)
    assert not any(args[:3] == ("gh", "ssh-key", "add") for args, _, _ in runtime.calls)
    assert runtime.changes == []


def test_ssh_key_operations_use_current_gh_account():
    import inspect

    source = inspect.getsource(ssh.reconcile)
    assert '["gh", "api", "user/keys"' in source
    assert '["gh", "ssh-key", "add"' in source
    assert "--repo" not in source


def test_ssh_repairs_managed_permissions(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    (ssh_dir / "config.d").mkdir(parents=True)
    key = ssh_dir / "id_ed25519_ai_github"
    public = key.with_suffix(".pub")
    key.write_text("private")
    public.write_text("ssh-ed25519 AAAATEST ai-github\n")
    fragment = ssh_dir / "config.d/ai-github.conf"
    fragment.write_text(ssh._config(tmp_path))
    known = ssh_dir / "known_hosts_ai_github"
    known.write_text(ssh.HOST_KEYS)
    config = ssh_dir / "config"
    config.write_text("Include config.d/*.conf\n")
    for path in (ssh_dir, ssh_dir / "config.d", key, fragment, known, config):
        path.chmod(0o777)
    effective = "\n".join(["identitiesonly yes", "stricthostkeychecking true", str(key), str(known)])
    responses = {
        ("ssh", "--version"): (0, "", ""), ("ssh-keygen", "--version"): (0, "", ""),
        ("gh", "--version"): (0, "", ""),
        ("ssh-keygen", "-y", "-f", str(key)): (0, "ssh-ed25519 AAAATEST\n", ""),
        ("ssh", "-G", "github.com"): (0, effective, ""),
        ("gh", "api", "user/keys", "--paginate", "--jq", ".[].key"):
            (0, "ssh-ed25519 AAAATEST\n", ""),
        ("ssh", "-T", "git@github.com"): (1, "", "successfully authenticated"),
    }
    runtime = FakeRuntime(tmp_path, responses)
    ssh.reconcile(runtime)
    assert key.stat().st_mode & 0o777 == 0o600
    assert ssh_dir.stat().st_mode & 0o777 == 0o700
    assert "repaired SSH permissions" in runtime.changes


def test_codex_profile_launchers_have_distinct_homes(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("x")
    for number in ("01", "02"):
        auth = tmp_path / f".local/share/ai/codex/{number}/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "access", "account_id": number, "refresh_token": "refresh"}}))
        launcher = tmp_path / f".local/bin/codex-{number}"
        runtime = FakeRuntime(tmp_path)
        codex._profile(runtime, number, binary)
    one = (tmp_path / ".local/bin/codex-01").read_text()
    two = (tmp_path / ".local/bin/codex-02").read_text()
    assert "/codex/01" in one and "/codex/02" not in one
    assert "/codex/02" in two and "/codex/01" not in two
    assert 'cli_auth_credentials_store="file"' in one
    runtime.changes.clear()
    codex._profile(runtime, "02", binary)
    assert runtime.changes == []


def test_interrupted_codex_authentication_can_retry(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("x")
    launcher = tmp_path / ".local/bin/codex-01"
    runtime = FakeRuntime(tmp_path, {(str(launcher), "login"): (1, "", "interrupted")})
    with pytest.raises(RuntimeError):
        codex._profile(runtime, "01", binary)
    marker = tmp_path / ".local/share/ai/codex/01/.authentication-in-progress"
    assert not marker.exists()
    auth = marker.parent / "auth.json"
    def succeed(argv, cwd):
        auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "access", "account_id": "account", "refresh_token": "refresh"}}))
        return (0, "", "")
    runtime.responses[(str(launcher), "login")] = succeed
    codex._profile(runtime, "01", binary)
    assert auth.exists()
