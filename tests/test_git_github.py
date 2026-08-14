import pytest

from ai.components import git, github
from conftest import FakeRuntime

GITHUB_IDENTITY_QUERY = ("gh", "api", "user", "--jq",
                         '[.name // "", .email // ""] | @tsv')


def test_git_healthy_rerun_does_nothing(tmp_path):
    responses = {
        ("git", "--version"): (0, "git version 2\n", ""),
        ("git", "config", "--global", "--get", "user.name"): (0, "Existing Name\n", ""),
        ("git", "config", "--global", "--get", "user.email"): (0, "existing@example.test\n", ""),
        ("git", "config", "--global", "--get", "init.defaultBranch"): (0, "main\n", ""),
    }
    runtime = FakeRuntime(tmp_path, responses)
    git.reconcile(runtime)
    assert runtime.changes == []
    assert not any(mutate for _, mutate, _ in runtime.calls)


def test_git_dry_run_does_not_prompt_or_mutate(tmp_path, monkeypatch):
    monkeypatch.setattr(git, "_request_identity",
                        lambda key: pytest.fail("dry-run prompted for identity"))
    runtime = FakeRuntime(tmp_path, {("git", "--version"): (0, "", "")}, dry_run=True)
    git.reconcile(runtime)
    mutations = [call[0] for call in runtime.calls if call[1]]
    assert mutations == [("git", "config", "--global", "init.defaultBranch", "main")]
    assert runtime.changes == ["configure user.name", "configure user.email",
                               "configured init.defaultBranch"]


def test_git_derives_only_missing_identity_from_authenticated_account(tmp_path):
    responses = {
        ("git", "--version"): (0, "", ""),
        ("git", "config", "--global", "--get", "user.name"): (0, "Existing Name\n", ""),
        ("git", "config", "--global", "--get", "user.email"): (1, "", ""),
        ("git", "config", "--global", "--get", "init.defaultBranch"): (0, "main\n", ""),
        ("gh", "auth", "status", "--hostname", "github.com"): (0, "", ""),
        GITHUB_IDENTITY_QUERY: (0, "Different Name\taccount@example.test\n", ""),
    }
    runtime = FakeRuntime(tmp_path, responses)

    def configured(argv, cwd):
        responses[("git", "config", "--global", "--get", "user.email")] = (
            0, "account@example.test\n", "")
        return 0, "", ""

    responses[("git", "config", "--global", "user.email", "account@example.test")] = configured
    git.reconcile(runtime)
    mutations = [args for args, mutate, _ in runtime.calls if mutate]
    assert mutations == [("git", "config", "--global", "user.email", "account@example.test")]


def test_git_requests_identity_when_github_does_not_expose_it(tmp_path, monkeypatch):
    responses = {
        ("git", "--version"): (0, "", ""),
        ("git", "config", "--global", "--get", "user.name"): (1, "", ""),
        ("git", "config", "--global", "--get", "user.email"): (0, "kept@example.test\n", ""),
        ("git", "config", "--global", "--get", "init.defaultBranch"): (0, "main\n", ""),
        ("gh", "auth", "status", "--hostname", "github.com"): (0, "", ""),
        GITHUB_IDENTITY_QUERY: (0, "\t\n", ""),
    }
    runtime = FakeRuntime(tmp_path, responses)
    monkeypatch.setattr(git, "_request_identity", lambda key: "Prompted Name")

    def configured(argv, cwd):
        responses[("git", "config", "--global", "--get", "user.name")] = (0, "Prompted Name\n", "")
        return 0, "", ""

    responses[("git", "config", "--global", "user.name", "Prompted Name")] = configured
    git.reconcile(runtime)
    assert ("git", "config", "--global", "user.name", "Prompted Name") in [
        args for args, mutate, _ in runtime.calls if mutate]


@pytest.mark.parametrize("login", ["first-account", "renamed-account", "other-account"])
def test_github_accepts_any_authenticated_account_without_reauthentication(tmp_path, login):
    responses = {
        ("gh", "--version"): (0, "", ""),
        ("gh", "auth", "status", "--hostname", "github.com"): (0, "", ""),
        ("gh", "api", "user", "--jq", ".login"): (0, login + "\n", ""),
        ("gh", "config", "get", "git_protocol", "--host", "github.com"): (0, "ssh\n", ""),
    }
    runtime = FakeRuntime(tmp_path, responses)
    github.reconcile(runtime)
    assert not any(args[:3] == ("gh", "auth", "login") for args, _, _ in runtime.calls)
    assert runtime.changes == []
