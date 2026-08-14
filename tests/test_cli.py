from ai import cli


def test_scoped_command_uses_only_requested_component(monkeypatch):
    called = []
    monkeypatch.setattr(cli.precheck, "reconcile", lambda runtime: called.append("precheck"))
    monkeypatch.setitem(cli.SCOPED, "git", ("Git", lambda runtime: called.append("git")))
    assert cli.main(["git", "--dry-run"]) == 0
    assert called == ["precheck", "git"]


def test_full_command_order(monkeypatch):
    called = []
    monkeypatch.setattr(cli.precheck, "reconcile", lambda runtime: called.append("precheck"))
    monkeypatch.setattr(cli, "COMPONENTS", [(name, lambda runtime, n=name: called.append(n))
                                             for name, _ in cli.COMPONENTS])
    assert cli.main(["--dry-run"]) == 0
    assert called == ["precheck", "Software", "Git", "GitHub", "SSH", "Codex", "Shell"]
