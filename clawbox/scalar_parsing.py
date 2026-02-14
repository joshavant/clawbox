from __future__ import annotations


def strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return value[:idx]
    return value


def parse_scalar(text: str, key: str) -> str:
    prefix = f"{key}:"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(prefix):
            continue

        raw_value = strip_inline_comment(line[len(prefix) :]).strip()
        if not raw_value:
            return ""
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
            return raw_value[1:-1].strip()
        return raw_value
    return ""
