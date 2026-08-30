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
    from scripts import hermes_korea_gateway as launcher
except ImportError:  # Direct execution from the scripts directory.
    import hermes_korea_gateway as launcher


class GatewayLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        launcher._close_gateway_egress_proxy()
        self.addCleanup(launcher._close_gateway_egress_proxy)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.test_root = Path(self.temp_dir.name)
        self.plugin_root = self.test_root / "plugins"
        self.hermes_root = self.test_root / "trusted-hermes"
        self.plugin_root.mkdir()
        self.hermes_root.mkdir()

    def _trusted(self, module: Any, name: str) -> Any:
        module.__file__ = str(self.hermes_root / name)
        return module

    def _base_module(self, base_class: Any = object) -> Any:
        return self._trusted(
            SimpleNamespace(BasePlatformAdapter=base_class),
            "gateway/platforms/base.py",
        )

    def test_actual_korea_plugin_contract_installs_on_hermes_base(self) -> None:
        class BasePlatformAdapter:
            async def handle_message(self, event: Any) -> Any:
                return event

        plugin_root = Path(__file__).resolve().parents[1] / "plugins"
        privacy = launcher.install_required_guard(
            plugin_root,
            base_class=BasePlatformAdapter,
        )

        self.assertTrue(
            privacy.is_location_ingress_guard_installed(
                base_class=BasePlatformAdapter
            )
        )

    def test_guard_is_verified_before_cli_runs(self) -> None:
        calls: List[Any] = []
        imports: List[Any] = []

        class BasePlatformAdapter:
            async def handle_message(self, event: Any) -> Any:
                return event

        privacy = SimpleNamespace(
            install_location_ingress_guard=(
                lambda **_: calls.append("install") or True
            ),
            is_location_ingress_guard_installed=(
                lambda **_: calls.append("assert") or True
            ),
        )

        def cli_main() -> int:
            calls.append(("cli", list(sys.argv)))
            return 7

        modules: Dict[str, Any] = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=cli_main),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": self._base_module(BasePlatformAdapter),
        }

        def importer(name: str) -> Any:
            imports.append((name, list(sys.argv)))
            return modules[name]

        result = launcher.main(
            ["gateway", "run"],
            plugin_root=self.plugin_root,
            hermes_root=self.hermes_root,
            importer=importer,
            privacy_loader=lambda _: privacy,
            browser_boundary=lambda: calls.append("browser"),
            toolset_verifier=lambda: calls.append("toolset"),
        )

        expected_argv = [
            "hermes",
            "gateway",
            "run",
            "--no-supervise",
            "--external-supervisor",
        ]
        self.assertEqual(result, 7)
        self.assertEqual(
            imports,
            [
                ("hermes_cli.main", expected_argv),
                ("gateway.platforms.base", expected_argv),
            ],
        )
        self.assertEqual(
            calls,
            ["browser", "install", "assert", "toolset", ("cli", expected_argv)],
        )

    def test_gateway_safety_flags_are_idempotent(self) -> None:
        arguments = launcher.enforce_same_process_gateway(
            [
                "gateway",
                "run",
                "--no-supervise",
                "--external-supervisor",
            ]
        )
        self.assertEqual(
            arguments,
            [
                "gateway",
                "run",
                "--no-supervise",
                "--external-supervisor",
            ],
        )

    def test_bare_gateway_is_made_an_explicit_safe_run(self) -> None:
        self.assertEqual(
            launcher.enforce_same_process_gateway(["gateway"]),
            [
                "gateway",
                "run",
                "--no-supervise",
                "--external-supervisor",
            ],
        )

    def test_profile_named_gateway_does_not_hide_real_subcommand(self) -> None:
        self.assertEqual(
            launcher.enforce_same_process_gateway(
                ["--profile", "gateway", "gateway", "run"]
            ),
            [
                "--profile",
                "gateway",
                "gateway",
                "run",
                "--no-supervise",
                "--external-supervisor",
            ],
        )

    def test_failed_install_never_runs_cli(self) -> None:
        imported: List[str] = []
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": self._base_module(),
        }
        privacy = SimpleNamespace(
            install_location_ingress_guard=lambda **_: False,
            is_location_ingress_guard_installed=lambda **_: False,
        )

        def importer(name: str) -> Any:
            imported.append(name)
            return modules[name]

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root,
                hermes_root=self.hermes_root,
                importer=importer,
                privacy_loader=lambda _: privacy,
                browser_boundary=lambda: None,
                toolset_verifier=lambda: None,
            )

        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertEqual(
            imported,
            ["hermes_cli.main", "gateway.platforms.base"],
        )
        self.assertIn("ingress guard", stderr.getvalue())

    def test_failed_assertion_never_runs_cli(self) -> None:
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": self._base_module(),
        }
        privacy = SimpleNamespace(
            install_location_ingress_guard=lambda **_: True,
            is_location_ingress_guard_installed=lambda **_: False,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root,
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                privacy_loader=lambda _: privacy,
                browser_boundary=lambda: None,
                toolset_verifier=lambda: None,
            )

        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertIn("ingress guard", stderr.getvalue())

    def test_missing_plugin_root_is_fail_closed(self) -> None:
        imported: List[str] = []
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": self._base_module(),
        }

        def importer(name: str) -> Any:
            imported.append(name)
            return modules[name]

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root / "missing",
                hermes_root=self.hermes_root,
                importer=importer,
                browser_boundary=lambda: None,
                toolset_verifier=lambda: None,
            )

        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertEqual(
            imported,
            ["hermes_cli.main", "gateway.platforms.base"],
        )
        self.assertIn("ingress guard", stderr.getvalue())

    def test_cli_import_outside_trusted_root_is_rejected(self) -> None:
        modules = {
            "hermes_cli.main": SimpleNamespace(
                main=lambda: self.fail("must not run"),
                __file__=str(self.test_root / "evil" / "hermes_cli.py"),
            )
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root,
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                browser_boundary=lambda: None,
                toolset_verifier=lambda: None,
            )

        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertIn("trusted Hermes runtime", stderr.getvalue())

    def test_gateway_base_import_outside_trusted_root_is_rejected(self) -> None:
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": SimpleNamespace(
                BasePlatformAdapter=object,
                __file__=str(self.test_root / "evil" / "base.py"),
            ),
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root,
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                browser_boundary=lambda: None,
                toolset_verifier=lambda: None,
            )

        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertIn("trusted Hermes runtime", stderr.getvalue())

    def test_plugin_registration_failure_never_runs_cli(self) -> None:
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": self._base_module(),
        }
        privacy = SimpleNamespace(
            install_location_ingress_guard=lambda **_: True,
            is_location_ingress_guard_installed=lambda **_: True,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root,
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                privacy_loader=lambda _: privacy,
                browser_boundary=lambda: None,
                toolset_verifier=lambda: (_ for _ in ()).throw(
                    RuntimeError("broken plugin")
                ),
            )
        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertIn("exact Korea plugin", stderr.getvalue())

    def test_browser_boundary_failure_never_installs_guard_or_runs_cli(self) -> None:
        calls: List[str] = []
        modules = {
            "hermes_cli.main": self._trusted(
                SimpleNamespace(main=lambda: self.fail("must not run")),
                "hermes_cli/main.py",
            ),
            "gateway.platforms.base": self._base_module(),
        }
        privacy = SimpleNamespace(
            install_location_ingress_guard=lambda **_: calls.append("install"),
            is_location_ingress_guard_installed=lambda **_: True,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                ["gateway", "run"],
                plugin_root=self.plugin_root,
                hermes_root=self.hermes_root,
                importer=modules.__getitem__,
                privacy_loader=lambda _: privacy,
                browser_boundary=lambda: (_ for _ in ()).throw(
                    RuntimeError("unsafe browser")
                ),
                toolset_verifier=lambda: None,
            )
        self.assertEqual(result, launcher.EX_CONFIG)
        self.assertEqual(calls, [])
        self.assertIn("private-network egress boundary", stderr.getvalue())

    def test_browser_boundary_sets_sentinel_and_checks_private_urls(self) -> None:
        prior = launcher.os.environ.get("TERMINAL_ENV")
        calls: List[Any] = []
        chromium = (
            self.hermes_root
            / ".playwright"
            / "chromium_headless_shell-test"
            / "chrome-headless-shell-linux64"
            / "chrome-headless-shell"
        )
        chromium.parent.mkdir(parents=True)
        chromium.touch()
        chromium.chmod(0o755)
        proxy = SimpleNamespace(
            proxy_url="http://127.0.0.1:32123",
            is_healthy=True,
            close=lambda: calls.append("proxy-close"),
        )

        def is_private(url: str) -> bool:
            return any(host in url for host in ("127.0.0.1", "10.", "172.20.", "192.168."))

        gateway_module = self._trusted(SimpleNamespace(), "gateway/run.py")
        browser_module = self._trusted(
            SimpleNamespace(
                _is_local_backend=lambda: (
                    launcher.os.environ.get("TERMINAL_ENV")
                    in {None, "", "local"}
                ),
                _allow_private_urls=lambda: True,
                _restrict_browser_evaluate=lambda: False,
                _allow_unsafe_browser_evaluate=lambda: True,
                _enforce_browser_eval_policy=lambda expression: (
                    "blocked" if "cookie" in expression else None
                ),
                evaluate_url_safety=lambda url: (
                    {"success": False, "error": "private"}
                    if is_private(url)
                    else None
                ),
                _eval_ssrf_guard_active=lambda _: (
                    launcher.os.environ.get("TERMINAL_ENV")
                    == launcher.GATEWAY_TERMINAL_SENTINEL
                ),
                _expression_targets_private_url=lambda expression: (
                    "http://127.0.0.1/private"
                    if "127.0.0.1" in expression
                    else None
                ),
                _build_browser_env=lambda: {
                    "PATH": "/bin",
                    "NO_PROXY": "127.0.0.1",
                    "HTTP_PROXY": "http://untrusted.invalid",
                    "AGENT_BROWSER_ARGS": "--no-proxy-server",
                    "BROWSER_CDP_URL": "ws://untrusted.invalid",
                },
                _needs_chromium_sandbox_bypass=lambda: True,
                _get_cdp_override_raw=lambda: "",
                _get_cloud_provider=lambda: None,
                _get_browser_engine=lambda: "chrome",
                _is_camofox_mode=lambda: False,
                _use_real_profile=lambda: False,
                _is_browser_use_cli_mode=lambda: False,
                check_browser_requirements=lambda: True,
                _active_sessions={},
            ),
            "tools/browser_tool.py",
        )

        def importer(name: str) -> Any:
            calls.append((name, launcher.os.environ.get("TERMINAL_ENV")))
            return {
                "gateway.run": gateway_module,
                "tools.browser_tool": browser_module,
            }[name]

        try:
            returned = launcher.install_gateway_browser_boundary(
                importer=importer,
                hermes_root=self.hermes_root,
                proxy_factory=lambda: proxy,
            )
            self.assertIs(returned, browser_module)
            self.assertFalse(browser_module._is_local_backend())
            self.assertFalse(browser_module._allow_private_urls())
            self.assertTrue(browser_module._restrict_browser_evaluate())
            self.assertFalse(browser_module._allow_unsafe_browser_evaluate())
            child_env = browser_module._build_browser_env()
            self.assertEqual(
                child_env["AGENT_BROWSER_PROXY"],
                "http://127.0.0.1:32123",
            )
            self.assertEqual(
                child_env["AGENT_BROWSER_PROXY_BYPASS"],
                "<-loopback>",
            )
            self.assertTrue(
                child_env["AGENT_BROWSER_NAMESPACE"].startswith("gw_")
            )
            self.assertEqual(
                child_env["AGENT_BROWSER_EXECUTABLE_PATH"],
                str(chromium.resolve()),
            )
            self.assertIn("--disable-quic", child_env["AGENT_BROWSER_ARGS"])
            self.assertIn("--no-sandbox", child_env["AGENT_BROWSER_ARGS"])
            self.assertNotIn("NO_PROXY", child_env)
            self.assertNotIn("HTTP_PROXY", child_env)
            self.assertNotIn("BROWSER_CDP_URL", child_env)
            session = browser_module._create_local_session("test")
            self.assertTrue(session["session_name"].startswith("gw_"))
            self.assertIsNone(session["cdp_url"])
            self.assertTrue(
                getattr(
                    browser_module._is_local_backend,
                    launcher._BROWSER_BOUNDARY_MARKER,
                    False,
                )
            )
            self.assertEqual(
                calls,
                [
                    ("gateway.run", prior),
                    (
                        "tools.browser_tool",
                        launcher.GATEWAY_TERMINAL_SENTINEL,
                    ),
                ],
            )
        finally:
            if prior is None:
                launcher.os.environ.pop("TERMINAL_ENV", None)
            else:
                launcher.os.environ["TERMINAL_ENV"] = prior

    def test_verify_required_toolset_rejects_partial_registration(self) -> None:
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
                resolve_toolset=lambda _: ["korea_place_search"],
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


if __name__ == "__main__":
    unittest.main()
