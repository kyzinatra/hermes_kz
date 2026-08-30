#!/usr/bin/env python3
"""Fail-closed launcher for the dedicated read-only Yandex Mail TUI.

Hermes intentionally treats plugin discovery failures as non-fatal and its TUI
falls back to the configured CLI toolsets when every explicit toolset is
invalid.  That is unsafe for untrusted email: a broken mail plugin must stop
the session, not silently expose terminal, browser, files, or Korea tools.

This launcher resolves the active Hermes profile, forces plugin discovery, and
requires the exact reviewed Yandex Mail toolset before the TUI starts.  It is
invoked through the image's normal entrypoint so the upstream bootstrap still
drops privileges to the ``hermes`` user.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


DEFAULT_HERMES_ROOT = Path("/opt/hermes")
EX_CONFIG = 78
EXPECTED_YANDEX_MAIL_TOOLS = frozenset(
    {
        "yandex_mail_list_inbox",
        "yandex_mail_read_message",
    }
)
_FATAL_RUNTIME_MESSAGE = (
    "FATAL: trusted Hermes mail runtime is unavailable; mail TUI will not start."
)
_FATAL_TOOLSET_MESSAGE = (
    "FATAL: exact read-only Yandex Mail toolset is unavailable; "
    "mail TUI will not start."
)


def _assert_module_origin(module: Any, trusted_root: Path, label: str) -> None:
    """Reject bootstrap modules imported outside the immutable Hermes tree."""
    source = getattr(module, "__file__", None)
    if not source:
        raise RuntimeError(f"{label} has no verifiable source path")
    root = Path(trusted_root).resolve()
    candidate = Path(source).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} loaded outside the trusted Hermes tree") from exc


def verify_required_toolset(
    *,
    importer: Callable[[str], Any] = importlib.import_module,
    hermes_root: Path = DEFAULT_HERMES_ROOT,
) -> None:
    """Force discovery and require exactly the two reviewed mail tools."""
    plugins_module = importer("hermes_cli.plugins")
    toolsets_module = importer("toolsets")
    _assert_module_origin(plugins_module, hermes_root, "Hermes plugin loader")
    _assert_module_origin(toolsets_module, hermes_root, "Hermes toolset registry")

    discover_plugins = getattr(plugins_module, "discover_plugins", None)
    validate_toolset = getattr(toolsets_module, "validate_toolset", None)
    resolve_toolset = getattr(toolsets_module, "resolve_toolset", None)
    if not all(callable(item) for item in (discover_plugins, validate_toolset, resolve_toolset)):
        raise RuntimeError("Hermes plugin verification contract is unavailable")

    discover_plugins(force=True)
    if validate_toolset("yandex_mail") is not True:
        raise RuntimeError("Yandex Mail toolset was not registered")
    resolved = frozenset(resolve_toolset("yandex_mail"))
    if resolved != EXPECTED_YANDEX_MAIL_TOOLS:
        raise RuntimeError("Yandex Mail toolset does not match the reviewed contract")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    hermes_root: Path = DEFAULT_HERMES_ROOT,
    importer: Callable[[str], Any] = importlib.import_module,
    toolset_verifier: Optional[Callable[[], None]] = None,
) -> int:
    """Verify the narrow toolset, then dispatch the official Hermes TUI."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--check"]):
        print(_FATAL_RUNTIME_MESSAGE, file=sys.stderr)
        return EX_CONFIG
    check_only = arguments == ["--check"]

    previous_argv = sys.argv
    sys.argv = ["hermes", "--toolsets", "yandex_mail", "--tui"]
    try:
        try:
            cli_module = importer("hermes_cli.main")
            _assert_module_origin(cli_module, hermes_root, "Hermes CLI")
            hermes_main = getattr(cli_module, "main", None)
            if not callable(hermes_main):
                raise RuntimeError("Hermes CLI entry point is unavailable")
        except Exception:  # noqa: BLE001 - fail closed without leaking internals
            print(_FATAL_RUNTIME_MESSAGE, file=sys.stderr)
            return EX_CONFIG

        try:
            if toolset_verifier is None:
                verify_required_toolset(
                    importer=importer,
                    hermes_root=hermes_root,
                )
            else:
                toolset_verifier()
        except Exception:  # noqa: BLE001 - fail closed without leaking internals
            print(_FATAL_TOOLSET_MESSAGE, file=sys.stderr)
            return EX_CONFIG

        if check_only:
            return 0
        result = hermes_main()
    finally:
        sys.argv = previous_argv
    return result if isinstance(result, int) and not isinstance(result, bool) else 0


if __name__ == "__main__":
    raise SystemExit(main())
