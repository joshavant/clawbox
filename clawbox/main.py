from __future__ import annotations

import argparse

from clawbox.cli import (
    add_optional_vm_number_args,
    add_profile_args,
    add_single_vm_number_arg,
    apply_profile_shortcuts,
    positive_int,
    resolve_optional_vm_number,
)
from clawbox.orchestrator import (
    ProvisionOptions,
    UpOptions,
    create_vm,
    delete_vm,
    down_vm,
    image_build,
    image_init,
    ip_vm,
    launch_vm,
    main_guard,
    provision_vm,
    recreate,
    status_environment,
    status_vm,
    up,
)


def _add_mount_path_args(parser: argparse.ArgumentParser, *, include_signal_payload: bool) -> None:
    parser.add_argument("--openclaw-source", default="")
    parser.add_argument("--openclaw-payload", default="")
    if include_signal_payload:
        parser.add_argument("--signal-cli-payload", default="")


def _add_optional_feature_flags(
    parser: argparse.ArgumentParser, *, include_enable_signal_payload: bool
) -> None:
    parser.add_argument("--add-playwright-provisioning", action="store_true")
    parser.add_argument("--add-tailscale-provisioning", action="store_true")
    parser.add_argument("--add-signal-cli-provisioning", action="store_true")
    if include_enable_signal_payload:
        parser.add_argument(
            "--enable-signal-payload",
            action="store_true",
            help=(
                "Enable signal payload sync mode (manual workflow: launch with "
                "--signal-cli-payload, then provision with this flag)"
            ),
        )


def _handle_create(args: argparse.Namespace, tart: object) -> None:
    create_vm(args.number, tart)


def _handle_launch(args: argparse.Namespace, tart: object) -> None:
    launch_vm(
        vm_number=args.number,
        profile=args.profile,
        openclaw_source=args.openclaw_source,
        openclaw_payload=args.openclaw_payload,
        signal_payload=args.signal_cli_payload,
        headless=args.headless,
        tart=tart,
    )


def _handle_provision(args: argparse.Namespace, tart: object) -> None:
    provision_vm(
        ProvisionOptions(
            vm_number=args.number,
            profile=args.profile,
            enable_playwright=args.add_playwright_provisioning,
            enable_tailscale=args.add_tailscale_provisioning,
            enable_signal_cli=args.add_signal_cli_provisioning,
            enable_signal_payload=args.enable_signal_payload,
        ),
        tart,
    )


def _handle_up(args: argparse.Namespace, tart: object) -> None:
    up(
        UpOptions(
            vm_number=args.number_final,
            profile=args.profile,
            openclaw_source=args.openclaw_source,
            openclaw_payload=args.openclaw_payload,
            signal_payload=args.signal_cli_payload,
            enable_playwright=args.add_playwright_provisioning,
            enable_tailscale=args.add_tailscale_provisioning,
            enable_signal_cli=args.add_signal_cli_provisioning,
        ),
        tart,
    )


def _handle_recreate(args: argparse.Namespace, tart: object) -> None:
    recreate(
        UpOptions(
            vm_number=args.number_final,
            profile=args.profile,
            openclaw_source=args.openclaw_source,
            openclaw_payload=args.openclaw_payload,
            signal_payload=args.signal_cli_payload,
            enable_playwright=args.add_playwright_provisioning,
            enable_tailscale=args.add_tailscale_provisioning,
            enable_signal_cli=args.add_signal_cli_provisioning,
        ),
        tart,
    )


def _handle_down(args: argparse.Namespace, tart: object) -> None:
    down_vm(args.number, tart)


def _handle_delete(args: argparse.Namespace, tart: object) -> None:
    delete_vm(args.number, tart)


def _handle_ip(args: argparse.Namespace, tart: object) -> None:
    ip_vm(args.number, tart)


def _handle_status(args: argparse.Namespace, tart: object) -> None:
    if args.number is None:
        status_environment(tart, as_json=getattr(args, "json", False))
        return
    status_vm(args.number, tart, as_json=getattr(args, "json", False))


def _handle_image_init(_: argparse.Namespace, __: object) -> None:
    image_init()


def _handle_image_build(args: argparse.Namespace, __: object) -> None:
    image_build(skip_init=args.skip_init, force=False)


def _handle_image_rebuild(args: argparse.Namespace, __: object) -> None:
    image_build(skip_init=args.skip_init, force=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawbox",
        description="Clawbox macOS VM orchestration",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create",
        help="Create a Clawbox VM from the base image",
    )
    add_single_vm_number_arg(create_parser)
    create_parser.set_defaults(handler=_handle_create)

    launch_parser = subparsers.add_parser(
        "launch",
        help="Launch a Clawbox VM",
    )
    add_single_vm_number_arg(launch_parser)
    add_profile_args(launch_parser)
    _add_mount_path_args(launch_parser, include_signal_payload=True)
    launch_parser.add_argument("--headless", action="store_true")
    launch_parser.set_defaults(handler=_handle_launch)

    provision_parser = subparsers.add_parser(
        "provision",
        help="Run provisioning on an existing VM",
    )
    add_single_vm_number_arg(provision_parser)
    add_profile_args(provision_parser)
    _add_optional_feature_flags(provision_parser, include_enable_signal_payload=True)
    provision_parser.set_defaults(handler=_handle_provision)

    up_parser = subparsers.add_parser(
        "up",
        help="Create, launch, and provision as needed",
    )
    add_optional_vm_number_args(up_parser)
    add_profile_args(up_parser)
    _add_mount_path_args(up_parser, include_signal_payload=True)
    _add_optional_feature_flags(up_parser, include_enable_signal_payload=False)
    up_parser.set_defaults(handler=_handle_up)

    recreate_parser = subparsers.add_parser(
        "recreate",
        help="Cleanly recreate a VM (down + delete + up)",
    )
    add_optional_vm_number_args(recreate_parser)
    add_profile_args(recreate_parser)
    _add_mount_path_args(recreate_parser, include_signal_payload=True)
    _add_optional_feature_flags(recreate_parser, include_enable_signal_payload=False)
    recreate_parser.set_defaults(handler=_handle_recreate)

    down_parser = subparsers.add_parser(
        "down",
        help="Stop a running Clawbox VM",
    )
    add_single_vm_number_arg(down_parser)
    down_parser.set_defaults(handler=_handle_down)

    delete_parser = subparsers.add_parser(
        "delete",
        help="Delete a Clawbox VM and local Clawbox state for that VM",
    )
    add_single_vm_number_arg(delete_parser)
    delete_parser.set_defaults(handler=_handle_delete)

    ip_parser = subparsers.add_parser(
        "ip",
        help="Print the VM IP address",
    )
    add_single_vm_number_arg(ip_parser)
    ip_parser.set_defaults(handler=_handle_ip)

    status_parser = subparsers.add_parser(
        "status",
        help="Show status for one VM or the full Clawbox environment",
    )
    status_parser.add_argument(
        "number",
        nargs="?",
        type=positive_int,
        help="Optional VM number (omit to show full environment status)",
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(handler=_handle_status)

    image_parser = subparsers.add_parser(
        "image",
        help="Manage the local macOS base image build",
    )
    image_subparsers = image_parser.add_subparsers(dest="image_command", required=True)

    image_init_parser = image_subparsers.add_parser(
        "init",
        help="Initialize packer plugins for the base image template",
    )
    image_init_parser.set_defaults(handler=_handle_image_init)

    image_build_parser = image_subparsers.add_parser(
        "build",
        help="Build the base image (runs image init first by default)",
    )
    image_build_parser.add_argument(
        "--skip-init",
        action="store_true",
        help="Skip packer init before build",
    )
    image_build_parser.set_defaults(handler=_handle_image_build)

    image_rebuild_parser = image_subparsers.add_parser(
        "rebuild",
        help="Force rebuild the base image (runs image init first by default)",
    )
    image_rebuild_parser.add_argument(
        "--skip-init",
        action="store_true",
        help="Skip packer init before rebuild",
    )
    image_rebuild_parser.set_defaults(handler=_handle_image_rebuild)

    return parser


def parse_args() -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args()

    if args.command in {"up", "recreate"}:
        args.number_final = resolve_optional_vm_number(args, parser)

    if hasattr(args, "developer") and hasattr(args, "standard"):
        apply_profile_shortcuts(args, parser)

    return args


def main() -> None:
    args = parse_args()

    def run(tart):
        handler = getattr(args, "handler", None)
        if handler is None:
            raise RuntimeError(f"Unhandled command: {getattr(args, 'command', '<missing>')}")
        handler(args, tart)

    main_guard(run)


if __name__ == "__main__":
    main()
