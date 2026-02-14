from __future__ import annotations

import argparse
import sys

import pytest

from clawbox import main as main_cli


def test_parse_up_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "up"])
    args = main_cli.parse_args()
    assert args.command == "up"
    assert args.number_final == 1
    assert args.profile == "standard"


def test_parse_recreate_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "recreate"])
    args = main_cli.parse_args()
    assert args.command == "recreate"
    assert args.number_final == 1
    assert args.profile == "standard"


def test_parse_up_signal_payload_does_not_imply_signal_cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["clawbox", "up", "--developer", "--signal-cli-payload", "/tmp/payload"],
    )
    args = main_cli.parse_args()
    assert args.command == "up"
    assert args.add_signal_cli_provisioning is False


def test_parse_recreate_signal_payload_does_not_imply_signal_cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["clawbox", "recreate", "--developer", "--signal-cli-payload", "/tmp/payload"],
    )
    args = main_cli.parse_args()
    assert args.command == "recreate"
    assert args.add_signal_cli_provisioning is False


def test_parse_launch_conflicting_profile_shortcuts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "launch", "--developer", "--standard"])
    with pytest.raises(SystemExit):
        main_cli.parse_args()


def test_parse_provision_enable_signal_payload_does_not_imply_signal_cli(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(sys, "argv", ["clawbox", "provision", "--enable-signal-payload"])
    args = main_cli.parse_args()
    assert args.command == "provision"
    assert args.enable_signal_payload is True
    assert args.add_signal_cli_provisioning is False


def test_parse_rejects_non_positive_vm_number(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "up", "0"])
    with pytest.raises(SystemExit):
        main_cli.parse_args()


def test_parse_rejects_non_positive_optional_vm_number(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "up", "--number", "-1"])
    with pytest.raises(SystemExit):
        main_cli.parse_args()


def test_main_dispatches_down(monkeypatch: pytest.MonkeyPatch):
    args = argparse.Namespace(handler=main_cli._handle_down, number=2)
    called: dict[str, object] = {}

    monkeypatch.setattr(main_cli, "parse_args", lambda: args)
    monkeypatch.setattr(
        main_cli,
        "main_guard",
        lambda fn: fn("fake-tart"),
    )
    monkeypatch.setattr(
        main_cli,
        "down_vm",
        lambda vm_number, tart: called.update({"vm_number": vm_number, "tart": tart}),
    )

    main_cli.main()

    assert called == {"vm_number": 2, "tart": "fake-tart"}


def test_main_dispatches_recreate(monkeypatch: pytest.MonkeyPatch):
    args = argparse.Namespace(
        handler=main_cli._handle_recreate,
        number_final=2,
        profile="standard",
        openclaw_source="",
        openclaw_payload="",
        signal_cli_payload="",
        add_playwright_provisioning=False,
        add_tailscale_provisioning=False,
        add_signal_cli_provisioning=False,
    )
    called: dict[str, object] = {}

    monkeypatch.setattr(main_cli, "parse_args", lambda: args)
    monkeypatch.setattr(
        main_cli,
        "main_guard",
        lambda fn: fn("fake-tart"),
    )
    monkeypatch.setattr(
        main_cli,
        "recreate",
        lambda opts, tart: called.update(
            {
                "vm_number": opts.vm_number,
                "profile": opts.profile,
                "openclaw_source": opts.openclaw_source,
                "openclaw_payload": opts.openclaw_payload,
                "signal_payload": opts.signal_payload,
                "tart": tart,
            }
        ),
    )

    main_cli.main()

    assert called == {
        "vm_number": 2,
        "profile": "standard",
        "openclaw_source": "",
        "openclaw_payload": "",
        "signal_payload": "",
        "tart": "fake-tart",
    }


def test_main_dispatches_ip(monkeypatch: pytest.MonkeyPatch):
    args = argparse.Namespace(handler=main_cli._handle_ip, number=1)
    called: dict[str, object] = {}

    monkeypatch.setattr(main_cli, "parse_args", lambda: args)
    monkeypatch.setattr(
        main_cli,
        "main_guard",
        lambda fn: fn("fake-tart"),
    )
    monkeypatch.setattr(
        main_cli,
        "ip_vm",
        lambda vm_number, tart: called.update({"vm_number": vm_number, "tart": tart}),
    )

    main_cli.main()

    assert called == {"vm_number": 1, "tart": "fake-tart"}


def test_main_dispatches_status(monkeypatch: pytest.MonkeyPatch):
    args = argparse.Namespace(handler=main_cli._handle_status, number=1, json=False)
    called: dict[str, object] = {}

    monkeypatch.setattr(main_cli, "parse_args", lambda: args)
    monkeypatch.setattr(
        main_cli,
        "main_guard",
        lambda fn: fn("fake-tart"),
    )
    monkeypatch.setattr(
        main_cli,
        "status_vm",
        lambda vm_number, tart, as_json=False: called.update(
            {"vm_number": vm_number, "tart": tart, "as_json": as_json}
        ),
    )

    main_cli.main()

    assert called == {"vm_number": 1, "tart": "fake-tart", "as_json": False}


def test_main_dispatches_status_environment(monkeypatch: pytest.MonkeyPatch):
    args = argparse.Namespace(handler=main_cli._handle_status, number=None, json=True)
    called: dict[str, object] = {}

    monkeypatch.setattr(main_cli, "parse_args", lambda: args)
    monkeypatch.setattr(
        main_cli,
        "main_guard",
        lambda fn: fn("fake-tart"),
    )
    monkeypatch.setattr(
        main_cli,
        "status_environment",
        lambda tart, as_json=False: called.update(
            {"tart": tart, "as_json": as_json}
        ),
    )

    main_cli.main()

    assert called == {"tart": "fake-tart", "as_json": True}


def test_parse_status_json(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "status", "2", "--json"])
    args = main_cli.parse_args()
    assert args.command == "status"
    assert args.number == 2
    assert args.json is True


def test_parse_status_without_number(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["clawbox", "status"])
    args = main_cli.parse_args()
    assert args.command == "status"
    assert args.number is None


def test_main_dispatches_image_build(monkeypatch: pytest.MonkeyPatch):
    args = argparse.Namespace(handler=main_cli._handle_image_build, skip_init=False)
    called: dict[str, object] = {}

    monkeypatch.setattr(main_cli, "parse_args", lambda: args)
    monkeypatch.setattr(
        main_cli,
        "main_guard",
        lambda fn: fn("fake-tart"),
    )
    monkeypatch.setattr(
        main_cli,
        "image_build",
        lambda skip_init, force: called.update(
            {"skip_init": skip_init, "force": force}
        ),
    )

    main_cli.main()

    assert called == {"skip_init": False, "force": False}
