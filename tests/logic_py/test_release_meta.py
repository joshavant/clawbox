from __future__ import annotations

from pathlib import Path

import pytest

from clawbox import release_meta


def _write_pyproject(path: Path, version: str) -> None:
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "clawbox"',
                f'version = "{version}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_validate_release_metadata_accepts_matching_version_and_changelog(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    _write_pyproject(pyproject, "1.0.0")
    changelog.write_text(
        "# Changelog\n\n## v1.0.0\n\n- Initial release.\n\n## v0.9.0\n\n- Previous.\n",
        encoding="utf-8",
    )

    section = release_meta.validate_release_metadata("v1.0.0", pyproject, changelog)
    assert section.startswith("## v1.0.0")
    assert "Initial release." in section
    assert "v0.9.0" not in section


def test_validate_release_metadata_rejects_mismatched_pyproject_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    _write_pyproject(pyproject, "1.0.1")
    changelog.write_text("# Changelog\n\n## v1.0.0\n\n- Initial release.\n", encoding="utf-8")

    with pytest.raises(release_meta.ReleaseMetaError, match="pyproject version mismatch"):
        release_meta.validate_release_metadata("v1.0.0", pyproject, changelog)


def test_validate_release_metadata_rejects_missing_changelog_section(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    _write_pyproject(pyproject, "1.0.0")
    changelog.write_text("# Changelog\n\n## v0.9.0\n\n- Previous.\n", encoding="utf-8")

    with pytest.raises(release_meta.ReleaseMetaError, match="Missing changelog section"):
        release_meta.validate_release_metadata("v1.0.0", pyproject, changelog)


def test_validate_version_tag_rejects_non_semver_tag() -> None:
    with pytest.raises(release_meta.ReleaseMetaError, match="Invalid version tag"):
        release_meta.validate_version_tag("1.0.0")
