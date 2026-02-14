from __future__ import annotations

from clawbox import services


def test_enabled_optional_service_keys_tracks_enabled_flags() -> None:
    enabled = services.enabled_optional_service_keys(
        enable_playwright=True,
        enable_tailscale=False,
        enable_signal_cli=True,
    )
    assert enabled == {services.SERVICE_PLAYWRIGHT, services.SERVICE_SIGNAL_CLI}


def test_unsupported_optional_services_returns_empty_for_standard_profile() -> None:
    enabled = services.enabled_optional_service_keys(
        enable_playwright=True,
        enable_tailscale=True,
        enable_signal_cli=True,
    )
    assert services.unsupported_optional_services("standard", enabled) == []

