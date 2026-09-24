import re
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "FPLDraftDash.py"


def test_season_year_is_global_parameter():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert re.search(r"^SEASON_YEAR = '\d{4}'$", source, re.MULTILINE)
    assert "DATA_DIR = BASE_DIR / SEASON_YEAR" in source


def test_all_pickle_paths_use_data_dir():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    pickle_lines = [
        line for line in source.splitlines()
        if ".pickle" in line and not line.strip().startswith("#")
    ]
    assert pickle_lines
    for line in pickle_lines:
        assert "DATA_DIR" in line, f"Pickle path not in season folder: {line.strip()}"
