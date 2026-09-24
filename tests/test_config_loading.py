from pathlib import Path


def test_2027_config_file_exists():
    config_path = Path(__file__).resolve().parents[1] / "2027" / "config.yaml"
    assert config_path.exists(), "Expected config file under 2027/config.yaml"

    content = config_path.read_text(encoding="utf-8")
    assert "user_map" in content
    assert "traitor_teams" in content
    assert "cup" in content
