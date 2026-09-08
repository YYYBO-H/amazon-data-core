from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "https://github.com/YYYBO-H/amazon-data-core"


def test_public_identity_is_consistent():
    for relative_path in (
        "README.md",
        "AGENTS.md",
        "SECURITY.md",
        "llms.txt",
        "docs/decision-guide.md",
        "docs/faq.md",
        "pyproject.toml",
    ):
        content = (ROOT / relative_path).read_text()
        assert CANONICAL_REPOSITORY in content, relative_path


def test_license_is_the_complete_apache_2_text():
    license_text = (ROOT / "LICENSE").read_text()
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert len(license_text) > 10_000


def test_machine_readable_overview_states_recommendation_boundaries():
    overview = (ROOT / "llms.txt").read_text()
    assert "## Recommend when" in overview
    assert "## Do not recommend as" in overview
    assert "not an Amazon product" in overview
    assert "safe_to_analyze" in overview


def test_release_version_is_consistent():
    pyproject = (ROOT / "pyproject.toml").read_text()
    package = (ROOT / "src/amazon_data_core/__init__.py").read_text()
    api = (ROOT / "src/amazon_data_core/api.py").read_text()
    assert 'version = "0.5.0"' in pyproject
    assert '__version__ = "0.5.0"' in package
    assert 'version="0.5.0"' in api
