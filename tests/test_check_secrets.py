"""Unit tests for the secret-leak prevention script."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_secrets  # noqa: E402


def test_blocks_dotenv_files(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OSMUND_API_KEY=placeholder\n", encoding="utf-8")

    env_local = tmp_path / ".env.local"
    env_local.write_text("OSMUND_API_KEY=placeholder\n", encoding="utf-8")

    violations = check_secrets.check_paths([env_file, env_local])
    assert len(violations) == 2


def test_allows_env_example_placeholder(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("OSMUND_API_KEY=osm_your_api_key_here\n", encoding="utf-8")

    assert check_secrets.check_paths([example]) == []


def test_detects_real_looking_osmund_api_key(tmp_path: Path) -> None:
    leaked = tmp_path / "notes.txt"
    synthetic_token = "osm_" + "A1b2C3d4E5f6G7h8I9j0"
    leaked.write_text(f"token = {synthetic_token}\n", encoding="utf-8")

    violations = check_secrets.check_paths([leaked])
    assert len(violations) == 1
    assert "possible Osmund API key detected" in violations[0]


def test_allows_short_key_prefix(tmp_path: Path) -> None:
    doc = tmp_path / "example.json"
    doc.write_text('{"prefix": "osm_4f9Kx2Qa"}\n', encoding="utf-8")

    assert check_secrets.check_paths([doc]) == []
