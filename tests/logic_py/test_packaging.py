from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


PROJECT_DIR = Path(__file__).resolve().parents[2]


def test_setuptools_data_files_reference_existing_files() -> None:
    if tomllib is None:
        pytest.skip("tomllib is unavailable on this interpreter")

    pyproject = tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = pyproject.get("tool", {}).get("setuptools", {}).get("data-files", {})

    missing: list[str] = []
    for destination, files in data_files.items():
        for file_path in files:
            if not (PROJECT_DIR / file_path).is_file():
                missing.append(f"{destination} -> {file_path}")

    assert not missing, (
        "pyproject.toml [tool.setuptools.data-files] contains missing files:\n"
        + "\n".join(sorted(missing))
    )
