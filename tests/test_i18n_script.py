"""CLI-level contract for the translation completeness checker."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_i18n_checker_cli_passes_for_the_repository():
    result = subprocess.run([sys.executable, "scripts/check_i18n.py"], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
