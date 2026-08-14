import pytest

from ai.components import shell
from ai.errors import AiError
from ai.runtime import Runtime
from conftest import FakeRuntime


def test_atomic_write_and_collision_protection(tmp_path):
    runtime = Runtime(home=tmp_path)
    target = tmp_path / ".config" / "managed"
    runtime.atomic_write(target, "one\n")
    runtime.atomic_write(target, "two\n")
    assert target.read_text() == "two\n"
    target.unlink()
    target.symlink_to(tmp_path / "victim")
    with pytest.raises(AiError):
        runtime.atomic_write(target, "bad")


def test_dry_run_atomic_write_is_non_mutating(tmp_path):
    Runtime(home=tmp_path, dry_run=True).atomic_write(tmp_path / "new", "data")
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_rejects_parent_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AiError):
        Runtime(home=tmp_path).atomic_write(tmp_path / "linked/managed", "bad")
    assert list(outside.iterdir()) == []


def test_shell_idempotent_and_missing_path(tmp_path):
    entry = str(tmp_path / ".local/bin")
    healthy = FakeRuntime(tmp_path, dry_run=True, responses={("fish", "--version"): (0, "", ""),
                                     ("fish", "-c", "string join \\n $fish_user_paths"): (0, entry + "\n", "")})
    shell.reconcile(healthy)
    assert healthy.changes == []
    missing = FakeRuntime(tmp_path, dry_run=True)
    shell.reconcile(missing)
    assert missing.changes == ["configured PATH"]
    assert sum(mutate for _, mutate, _ in missing.calls) == 1
    command = [call for call in missing.calls if call[1]][0][0]
    assert entry not in command[-1]
    assert "--path" not in command[-1]


def test_shell_accepts_string_join_status_one_when_path_is_present(tmp_path):
    entry = str(tmp_path / ".local/bin")
    query = ("fish", "-c", "string join \\n $fish_user_paths")
    responses = {("fish", "--version"): (0, "", ""), query: (1, entry + "\n", "")}
    runtime = FakeRuntime(tmp_path, responses)
    shell.reconcile(runtime)
    assert runtime.changes == []


def test_shell_dry_run_reads_current_fish_variables_location(tmp_path):
    variables = tmp_path / ".config/fish/fish_variables"
    variables.parent.mkdir(parents=True)
    variables.write_text("# VERSION: 3.0\nSETUVAR fish_user_paths:/managed/path\n")
    runtime = FakeRuntime(tmp_path, dry_run=True, responses={
        ("fish", "--version"): (0, "fish, version 4\n", ""),
        ("fish", "-c", "string join \\n $fish_user_paths"): (1, str(tmp_path / ".local/bin") + "\n", ""),
    })
    shell.reconcile(runtime)
    assert runtime.changes == []
