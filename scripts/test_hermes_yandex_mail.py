from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

try:
    from scripts import hermes_yandex_mail as launcher
except ImportError:  # Direct execution from the scripts directory.
    import hermes_yandex_mail as launcher


class YandexMailLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.hermes_root = Path(self.temp_dir.name) / "trusted-hermes"
        self.hermes_root.mkdir()

    def _trusted(self, module: Any, name: str) -> Any:
        module.__file__ = str(self.hermes_root / name)
        return module

    def test_verifies_before_tui_and_forces_exact_arguments(self) -> None:
        calls: List[Any] = []

        def cli_main() -> int:
            calls.append(("cli", list(sys.argv)))
            return 9

        modules: Dict[str, Any] = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=cli_main),
                "hermes_cli/main.py",
            )
        }

        result = launcher.main(
            [],
            hermes_root=self.hermes_root,
            importer=modules.__getitem__,
            toolset_verifier=lambda: calls.append("verify"),
        )

        self.assertEqual(result, 9)
        self.assertEqual(
            calls,
            [
                "verify",
                (
                    "cli",
                    ["hermes", "--toolsets", "yandex_mail", "--tui"],
                ),
            ],
        )

    def test_check_mode_never_starts_tui(self) -> None:
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            )
        }
        self.assertEqual(
            launcher.main(
                ["--check"],
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                toolset_verifier=lambda: None,
            ),
            0,
        )

    def test_discovery_failure_is_fail_closed(self) -> None:
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            )
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                [],
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                toolset_verifier=lambda: (_ for _ in ()).throw(
                    RuntimeError("broken plugin")
                ),
            )
        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertIn("exact narrow", stderr.getvalue())

    def test_verify_required_toolset_rejects_missing_plugin(self) -> None:
        plugins = self._trusted(
            SimpleNamespace(discover_plugins=lambda **_: None),
            "hermes_cli/plugins.py",
        )
        toolsets = self._trusted(
            SimpleNamespace(
                validate_toolset=lambda _: False,
                resolve_toolset=lambda _: [],
            ),
            "toolsets.py",
        )
        with self.assertRaises(RuntimeError):
            launcher.verify_required_toolset(
                importer={
                    "hermes_cli.plugins": plugins,
                    "toolsets": toolsets,
                }.__getitem__,
                hermes_root=self.hermes_root,
            )

    def test_verify_required_toolset_rejects_extra_tool(self) -> None:
        calls: List[Any] = []
        plugins = self._trusted(
            SimpleNamespace(
                discover_plugins=lambda **kwargs: calls.append(kwargs)
            ),
            "hermes_cli/plugins.py",
        )
        toolsets = self._trusted(
            SimpleNamespace(
                validate_toolset=lambda _: True,
                resolve_toolset=lambda _: [
                    *launcher.EXPECTED_YANDEX_MAIL_TOOLS,
                    "terminal",
                ],
            ),
            "toolsets.py",
        )
        with self.assertRaises(RuntimeError):
            launcher.verify_required_toolset(
                importer={
                    "hermes_cli.plugins": plugins,
                    "toolsets": toolsets,
                }.__getitem__,
                hermes_root=self.hermes_root,
            )
        self.assertEqual(calls, [{"force": True}])

    def test_untrusted_cli_module_is_rejected(self) -> None:
        modules = {
            "hermes_cli.main": SimpleNamespace(
                main=lambda: self.fail("must not run"),
                __file__=str(self.hermes_root.parent / "evil.py"),
            )
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                [],
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                toolset_verifier=lambda: None,
            )
        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertIn("trusted Hermes mail runtime", stderr.getvalue())

    def test_arbitrary_arguments_are_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                launcher.main(["--toolsets", "all"]),
                launcher.EX_CONFIG,
            )


if __name__ == "__main__":
    unittest.main()
