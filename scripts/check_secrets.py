#!/usr/bin/env python3
"""Check repository files for leaked Osmund API keys or secret files."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_FILENAMES = {
    ".env",
    "secrets.yaml",
}

ALLOWED_PLACEHOLDERS = {
    "osm_your_api_key_here",
}

# Real Osmund API keys start with `osm_` and are significantly longer than the
# 12-character key prefix (e.g. `osm_4f9Kx2Qa`) returned by `/api/v1/me`.
OSMUND_KEY_PATTERN = re.compile(r"\bosm_[A-Za-z0-9_-]{16,}\b")


def _git_files(staged_only: bool) -> list[Path]:
    cmd = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
        if staged_only
        else ["git", "ls-files"]
    )
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def _is_forbidden_path(path: Path) -> bool:
    name = path.name
    if name in FORBIDDEN_FILENAMES:
        return True
    return name.startswith(".env.") and name != ".env.example"


def check_paths(paths: list[Path]) -> list[str]:
    """Return a list of secret-check violation messages."""
    violations: list[str] = []
    for path in paths:
        if _is_forbidden_path(path):
            violations.append(
                f"{path}: secret file must not be committed (add to .gitignore)"
            )
            continue

        if not path.is_file():
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            for match in OSMUND_KEY_PATTERN.finditer(line):
                token = match.group(0)
                if token in ALLOWED_PLACEHOLDERS:
                    continue
                violations.append(
                    f"{path}:{line_no}: possible Osmund API key detected "
                    f"({token[:8]}...)"
                )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify no Osmund API keys or secret files are committed."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Check only git-staged files (for use in a pre-commit hook).",
    )
    args = parser.parse_args()

    paths = _git_files(staged_only=args.staged)
    violations = check_paths(paths)
    if violations:
        print("Secret check failed:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print(f"Secret check passed ({len(paths)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
