from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OptionalServiceSpec:
    key: str
    display_name: str
    cli_flag: str
    allowed_profiles: frozenset[str]


SERVICE_PLAYWRIGHT = "playwright"
SERVICE_TAILSCALE = "tailscale"
SERVICE_SIGNAL_CLI = "signal_cli"

OPTIONAL_SERVICES: tuple[OptionalServiceSpec, ...] = (
    OptionalServiceSpec(
        key=SERVICE_PLAYWRIGHT,
        display_name="Playwright",
        cli_flag="--add-playwright-provisioning",
        allowed_profiles=frozenset({"standard", "developer"}),
    ),
    OptionalServiceSpec(
        key=SERVICE_TAILSCALE,
        display_name="Tailscale",
        cli_flag="--add-tailscale-provisioning",
        allowed_profiles=frozenset({"standard", "developer"}),
    ),
    OptionalServiceSpec(
        key=SERVICE_SIGNAL_CLI,
        display_name="signal-cli",
        cli_flag="--add-signal-cli-provisioning",
        allowed_profiles=frozenset({"standard", "developer"}),
    ),
)

OPTIONAL_SERVICE_BY_KEY = {spec.key: spec for spec in OPTIONAL_SERVICES}


def enabled_optional_service_keys(
    *,
    enable_playwright: bool,
    enable_tailscale: bool,
    enable_signal_cli: bool,
) -> set[str]:
    enabled: set[str] = set()
    if enable_playwright:
        enabled.add(SERVICE_PLAYWRIGHT)
    if enable_tailscale:
        enabled.add(SERVICE_TAILSCALE)
    if enable_signal_cli:
        enabled.add(SERVICE_SIGNAL_CLI)
    return enabled


def unsupported_optional_services(profile: str, enabled_keys: set[str]) -> list[OptionalServiceSpec]:
    unsupported: list[OptionalServiceSpec] = []
    for key in sorted(enabled_keys):
        spec = OPTIONAL_SERVICE_BY_KEY.get(key)
        if spec is None:
            continue
        if profile not in spec.allowed_profiles:
            unsupported.append(spec)
    return unsupported

