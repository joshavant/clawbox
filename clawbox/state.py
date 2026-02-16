from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ProvisionMarker:
    vm_name: str
    profile: str
    playwright: bool
    tailscale: bool
    signal_cli: bool
    signal_payload: bool
    provisioned_at: str
    sync_backend: str = "mutagen"

    @classmethod
    def from_file(cls, marker_file: Path) -> "ProvisionMarker | None":
        if not marker_file.exists():
            return None
        data: dict[str, str] = {}
        for line in marker_file.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
        if not data:
            return None

        def b(name: str) -> bool:
            return data.get(name, "false") == "true"

        return cls(
            vm_name=data.get("vm_name", ""),
            profile=data.get("profile", ""),
            playwright=b("playwright"),
            tailscale=b("tailscale"),
            signal_cli=b("signal_cli"),
            signal_payload=b("signal_payload"),
            sync_backend=data.get("sync_backend", ""),
            provisioned_at=data.get("provisioned_at", ""),
        )

    def write(self, marker_file: Path) -> None:
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        marker_file.write_text(
            "\n".join(
                [
                    f"vm_name: {self.vm_name}",
                    f"profile: {self.profile}",
                    f"playwright: {'true' if self.playwright else 'false'}",
                    f"tailscale: {'true' if self.tailscale else 'false'}",
                    f"signal_cli: {'true' if self.signal_cli else 'false'}",
                    f"signal_payload: {'true' if self.signal_payload else 'false'}",
                    f"sync_backend: {self.sync_backend}",
                    f"provisioned_at: {self.provisioned_at}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )


def current_utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
