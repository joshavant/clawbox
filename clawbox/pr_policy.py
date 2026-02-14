from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]

CONVENTIONAL_PR_TITLE_PATTERN = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: .+"
)


class PrPolicyError(RuntimeError):
    """Raised when a PR policy check fails."""


def valid_pr_title(title: str) -> bool:
    return bool(CONVENTIONAL_PR_TITLE_PATTERN.match(title.strip()))


def ensure_pr_title_valid(title: str) -> None:
    if valid_pr_title(title):
        return
    raise PrPolicyError(
        "PR title must follow Conventional Commits, for example: "
        "'feat: add X', 'fix(cli): correct Y', or 'refactor!: remove Z'."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts/pr", description="Local PR policy workflow helpers.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_title_cmd = subparsers.add_parser("check-title")
    check_title_cmd.add_argument("--title", required=True, help="PR title to validate")

    validate_cmd = subparsers.add_parser("validate")
    validate_cmd.add_argument("--title", required=True, help="PR title to validate")

    prepare_cmd = subparsers.add_parser("prepare")
    prepare_cmd.add_argument("--title", default=None, help="Optional PR title to validate")
    prepare_cmd.add_argument("--skip-fast", action="store_true", help="Skip ./scripts/ci/run.sh fast")
    prepare_cmd.add_argument("--skip-logic", action="store_true", help="Skip ./scripts/ci/run.sh logic")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "check-title":
            ensure_pr_title_valid(args.title)
            print("PR title check passed.")
            return 0

        if args.command == "validate":
            ensure_pr_title_valid(args.title)
            print("PR policy checks passed.")
            return 0

        if args.command == "prepare":
            if args.title:
                ensure_pr_title_valid(args.title)
            if not args.skip_fast:
                subprocess.run(["./scripts/ci/run.sh", "fast"], cwd=PROJECT_DIR, check=True)
            if not args.skip_logic:
                subprocess.run(["./scripts/ci/run.sh", "logic"], cwd=PROJECT_DIR, check=True)
            print("PR prepare checks passed.")
            return 0

        parser.error(f"unknown command: {args.command}")
    except PrPolicyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode if isinstance(exc.returncode, int) else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
