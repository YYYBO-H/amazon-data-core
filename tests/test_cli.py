from pathlib import Path

from amazon_data_core.cli import install_skill


def test_install_skill_writes_a_standard_skill(tmp_path: Path):
    result = install_skill("generic", tmp_path)
    skill = tmp_path / "amazon-data-core" / "SKILL.md"

    assert result == {"installed": str(skill.parent), "host": "generic"}
    assert skill.exists()
    assert "amazon_data_health" in skill.read_text()
