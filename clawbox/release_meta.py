from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ReleaseMetaError(RuntimeError):
    """Raised when release metadata checks fail."""


def validate_version_tag(version_tag: str) -> str:
    if not VERSION_TAG_PATTERN.fullmatch(version_tag.strip()):
        raise ReleaseMetaError(f"Invalid version tag '{version_tag}'. Expected format: vX.Y.Z")
    return version_tag.strip()


def expected_project_version(version_tag: str) -> str:
    validate_version_tag(version_tag)
    return version_tag[1:]


def read_project_version(pyproject_path: Path) -> str:
    content = pyproject_path.read_text(encoding="utf-8")
    in_project_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project_section = line == "[project]"
            continue
        if in_project_section and line.startswith("version"):
            match = re.match(r'version\s*=\s*"([^"]+)"', line)
            if match:
                return match.group(1)
            break
    raise ReleaseMetaError(f"Could not read [project].version from {pyproject_path}")


def extract_changelog_section(version_tag: str, changelog_path: Path) -> str:
    heading = f"## {version_tag}"
    lines = changelog_path.read_text(encoding="utf-8").splitlines()

    start_index: int | None = None
    for idx, line in enumerate(lines):
        if line.strip() == heading:
            start_index = idx
            break

    if start_index is None:
        raise ReleaseMetaError(f"Missing changelog section heading: {heading}")

    end_index = len(lines)
    for idx in range(start_index + 1, len(lines)):
        if lines[idx].startswith("## "):
            end_index = idx
            break

    section = "\n".join(lines[start_index:end_index]).strip()
    if not section:
        raise ReleaseMetaError(f"Changelog section for {version_tag} is empty")
    return section + "\n"


def validate_release_metadata(version_tag: str, pyproject_path: Path, changelog_path: Path) -> str:
    normalized_tag = validate_version_tag(version_tag)
    project_version = read_project_version(pyproject_path)
    expected = expected_project_version(normalized_tag)
    if project_version != expected:
        raise ReleaseMetaError(
            f"pyproject version mismatch: expected {expected} for tag {normalized_tag}, found {project_version}"
        )
    return extract_changelog_section(normalized_tag, changelog_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release-meta", description="Release metadata validation helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--version", required=True, help="Release tag version (vX.Y.Z)")
    common.add_argument("--pyproject", default="pyproject.toml", help="Path to pyproject.toml")
    common.add_argument("--changelog", default="CHANGELOG.md", help="Path to CHANGELOG.md")

    subparsers.add_parser("check", parents=[common], help="Validate release metadata")

    notes = subparsers.add_parser("notes", parents=[common], help="Write release notes from changelog section")
    notes.add_argument("--output", required=True, help="Output file for release notes")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        pyproject_path = Path(args.pyproject)
        changelog_path = Path(args.changelog)
        section = validate_release_metadata(args.version, pyproject_path, changelog_path)

        if args.command == "check":
            print(
                "Release metadata check passed "
                f"(version={args.version}, pyproject={pyproject_path}, changelog={changelog_path})."
            )
            return 0

        if args.command == "notes":
            output_path = Path(args.output)
            output_path.write_text(section, encoding="utf-8")
            print(f"Wrote release notes: {output_path}")
            return 0

        parser.error(f"unknown command: {args.command}")
    except ReleaseMetaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
