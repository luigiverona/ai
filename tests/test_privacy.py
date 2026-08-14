from pathlib import Path


def test_production_sources_have_no_fixed_user_identity():
    repo = Path(__file__).parents[1]
    production = [repo / "install", repo / "README.md", *sorted((repo / "src").rglob("*.py"))]
    text = "\n".join(path.read_text() for path in production)
    assert "@gmail.com" not in text.lower()
    assert "ACCOUNT =" not in text
    assert "github.com/" not in (repo / "install").read_text()
    assert "/home/" not in text
