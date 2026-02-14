from __future__ import annotations

import subprocess
import sys
from typing import Callable

from clawbox.tart import TartClient, TartError


class UserFacingError(RuntimeError):
    """Error with user-facing text; caller should print and return non-zero."""


def main_guard(fn: Callable[[TartClient], None]) -> None:
    """Run fn and convert known errors to CLI output/exit code."""
    tart = TartClient()
    try:
        fn(tart)
    except (UserFacingError, TartError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except FileNotFoundError as exc:
        print(f"Error: Command not found: {exc.filename or 'unknown'}", file=sys.stderr)
        raise SystemExit(1) from exc
    except subprocess.SubprocessError as exc:
        print(f"Error: Command execution failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except OSError as exc:
        print(f"Error: OS command failure: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
