from __future__ import annotations

from pathlib import Path

import pytest

from clawbox import release_formula


def test_render_formula_uses_release_archive_url_and_sha() -> None:
    rendered = release_formula.render_formula(
        "v1.2.3",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )
    assert 'url "https://github.com/joshavant/clawbox/releases/download/v1.2.3/clawbox-1.2.3.tar.gz"' in rendered
    assert 'sha256 "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"' in rendered
    assert 'version "1.2.3"' in rendered
    assert 'head "https://github.com/joshavant/clawbox.git", branch: "main"' in rendered


def test_render_formula_rejects_invalid_version_tag() -> None:
    with pytest.raises(release_formula.ReleaseFormulaError):
        release_formula.render_formula(
            "1.2.3",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )


def test_render_formula_rejects_invalid_sha256() -> None:
    with pytest.raises(release_formula.ReleaseFormulaError):
        release_formula.render_formula("v1.2.3", "abc123")


def test_update_formula_file_writes_formula(tmp_path: Path) -> None:
    formula_path = tmp_path / "clawbox.rb"
    release_formula.update_formula_file(
        formula_path,
        "v1.0.0",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    content = formula_path.read_text(encoding="utf-8")
    assert content.startswith("class Clawbox < Formula")
    assert 'version "1.0.0"' in content
