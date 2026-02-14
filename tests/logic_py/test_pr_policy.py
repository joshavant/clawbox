from __future__ import annotations

from clawbox import pr_policy


def test_valid_pr_title_accepts_conventional_commits() -> None:
    assert pr_policy.valid_pr_title("feat: add command")
    assert pr_policy.valid_pr_title("fix(cli): handle edge case")
    assert pr_policy.valid_pr_title("refactor!: remove deprecated flag")


def test_valid_pr_title_rejects_non_conventional_titles() -> None:
    assert not pr_policy.valid_pr_title("update readme")
    assert not pr_policy.valid_pr_title("feat add command")
    assert not pr_policy.valid_pr_title("Feature: add command")
