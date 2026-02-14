from __future__ import annotations

import argparse


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("VM number must be >= 1")
    return parsed


def add_profile_args(parser: argparse.ArgumentParser, default: str = "standard") -> None:
    parser.add_argument("--profile", choices=("standard", "developer"), default=default)
    parser.add_argument("--developer", action="store_true", help="Shortcut for --profile developer")
    parser.add_argument("--standard", action="store_true", help="Shortcut for --profile standard")


def apply_profile_shortcuts(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if getattr(args, "developer", False) and getattr(args, "standard", False):
        parser.error("--developer and --standard are mutually exclusive")
    if getattr(args, "developer", False):
        args.profile = "developer"
    if getattr(args, "standard", False):
        args.profile = "standard"


def add_single_vm_number_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "number",
        nargs="?",
        type=positive_int,
        default=1,
        help="VM number (default: 1)",
    )


def add_optional_vm_number_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("number_pos", nargs="?", type=positive_int, help="Optional VM number")
    parser.add_argument("--number", type=positive_int, help="Optional VM number")


def resolve_optional_vm_number(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int:
    if args.number is not None and args.number_pos is not None:
        parser.error("VM number provided more than once")
    if args.number is not None:
        return args.number
    if args.number_pos is not None:
        return args.number_pos
    return 1
