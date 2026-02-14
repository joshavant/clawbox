from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from clawbox.release_meta import ReleaseMetaError, validate_version_tag


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReleaseFormulaError(RuntimeError):
    """Raised when formula update inputs are invalid."""


def validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise ReleaseFormulaError(f"Invalid sha256 '{value}'. Expected 64 lowercase hex characters.")
    return normalized


def render_formula(version_tag: str, sha256: str) -> str:
    try:
        validated_tag = validate_version_tag(version_tag)
    except ReleaseMetaError as exc:
        raise ReleaseFormulaError(str(exc)) from exc
    validated_sha = validate_sha256(sha256)
    version = validated_tag[1:]
    archive = f"clawbox-{version}.tar.gz"
    url = f"https://github.com/joshavant/clawbox/releases/download/{validated_tag}/{archive}"

    return (
        "class Clawbox < Formula\n"
        "  include Language::Python::Virtualenv\n"
        "\n"
        "  desc \"Provision and manage Clawbox macOS VMs with Tart\"\n"
        "  homepage \"https://github.com/joshavant/clawbox\"\n"
        f"  url \"{url}\"\n"
        f"  sha256 \"{validated_sha}\"\n"
        f"  version \"{version}\"\n"
        "  license \"MIT\"\n"
        "  head \"https://github.com/joshavant/clawbox.git\", branch: \"main\"\n"
        "\n"
        "  depends_on \"python@3.12\"\n"
        "  depends_on \"ansible\"\n"
        "  depends_on \"hashicorp/tap/packer\"\n"
        "  depends_on \"cirruslabs/cli/tart\"\n"
        "\n"
        "  def install\n"
        "    virtualenv_install_with_resources\n"
        "  end\n"
        "\n"
        "  test do\n"
        "    output = shell_output(\"#{bin}/clawbox --help\")\n"
        "    assert_match \"Clawbox macOS VM orchestration\", output\n"
        "  end\n"
        "end\n"
    )


def update_formula_file(formula_path: Path, version_tag: str, sha256: str) -> None:
    formula_path.write_text(render_formula(version_tag, sha256), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release-formula", description="Update Homebrew formula for a release")
    parser.add_argument("--formula", default="Formula/clawbox.rb", help="Path to formula file")
    parser.add_argument("--version", required=True, help="Release tag version (vX.Y.Z)")
    parser.add_argument("--sha256", required=True, help="SHA256 for release tarball")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        update_formula_file(Path(args.formula), args.version, args.sha256)
    except (ReleaseFormulaError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Updated formula: {args.formula}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
