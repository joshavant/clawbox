from __future__ import annotations

import ast
import re
from pathlib import Path


RESERVED_TEST_VM_NUMBERS = set(range(90, 100))
TESTS_DIR = Path(__file__).resolve().parents[1]
VM_NAME_RE = re.compile(r"clawbox-(\d+)(?!\.\d)")
VM_NUMBER_KEYWORDS = {
    "vm_number",
    "number",
    "number_final",
    "standard_vm_number",
    "developer_vm_number",
    "optional_vm_number",
}
VM_NUMBER_CALLS = {
    "create_vm",
    "launch_vm",
    "provision_vm",
    "up",
    "recreate",
    "down_vm",
    "delete_vm",
    "ip_vm",
    "status_vm",
    "vm_name_for",
}
VM_NUMBER_ENV_SUFFIX = "_VM_NUMBER"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _scan_vm_name_literals(path: Path, text: str) -> list[str]:
    violations: list[str] = []
    for match in VM_NAME_RE.finditer(text):
        vm_number = int(match.group(1))
        if vm_number in RESERVED_TEST_VM_NUMBERS:
            continue
        line = _line_number(text, match.start())
        violations.append(f"{path}:{line} uses non-reserved VM name '{match.group(0)}'")
    return violations


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _scan_vm_number_constants(path: Path, tree: ast.AST) -> list[str]:
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        for keyword in node.keywords:
            if keyword.arg not in VM_NUMBER_KEYWORDS:
                continue
            if not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, int):
                continue
            vm_number = keyword.value.value
            if vm_number in RESERVED_TEST_VM_NUMBERS:
                continue
            violations.append(
                f"{path}:{keyword.value.lineno} uses non-reserved {keyword.arg}={vm_number}"
            )

        call_name = _call_name(node)
        if call_name not in VM_NUMBER_CALLS:
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, int):
            continue
        vm_number = first_arg.value
        if vm_number in RESERVED_TEST_VM_NUMBERS:
            continue
        violations.append(f"{path}:{first_arg.lineno} calls {call_name} with non-reserved VM number {vm_number}")

    return violations


def _scan_vm_env_defaults(path: Path, tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node)
        if call_name != "vm_i":
            continue
        if len(node.args) < 2:
            continue
        env_name_node = node.args[0]
        default_node = node.args[1]
        if not isinstance(env_name_node, ast.Constant) or not isinstance(env_name_node.value, str):
            continue
        if not env_name_node.value.endswith(VM_NUMBER_ENV_SUFFIX):
            continue
        if not isinstance(default_node, ast.Constant) or not isinstance(default_node.value, int):
            continue
        vm_number = default_node.value
        if vm_number in RESERVED_TEST_VM_NUMBERS:
            continue
        violations.append(
            f"{path}:{default_node.lineno} uses non-reserved default for {env_name_node.value}: {vm_number}"
        )
    return violations


def test_all_tests_use_reserved_vm_numbers_only() -> None:
    violations: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        violations.extend(_scan_vm_name_literals(path, text))
        violations.extend(_scan_vm_number_constants(path, tree))
        violations.extend(_scan_vm_env_defaults(path, tree))

    assert not violations, "VM number policy violations:\n" + "\n".join(violations)
