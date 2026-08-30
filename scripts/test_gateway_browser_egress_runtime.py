from __future__ import annotations

import http.server
import importlib
import json
import os
import socket
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from tools import browser_tool as browser
except ImportError:  # Host unit runs intentionally lack the pinned runtime.
    browser = None

try:
    from scripts import hermes_korea_gateway as launcher
    from scripts.gateway_egress_proxy import GatewayEgressProxy
except ImportError:
    import hermes_korea_gateway as launcher
    from gateway_egress_proxy import GatewayEgressProxy


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


class _PrivateHandler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits += 1
        body = b"private side effect"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


class _PublicHandler(http.server.BaseHTTPRequestHandler):
    hits = 0
    private_port = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits += 1
        self.send_response(302)
        self.send_header(
            "Location",
            f"http://127.0.0.1:{type(self).private_port}/private",
        )
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> tuple[_QuietServer, threading.Thread]:
    server = _QuietServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@unittest.skipIf(browser is None or os.name != "posix", "requires pinned Linux Hermes runtime")
class GatewayBrowserEgressRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        launcher._close_gateway_egress_proxy()
        browser.cleanup_all_browsers()
        _PrivateHandler.hits = 0
        _PublicHandler.hits = 0

        self.private_server, self.private_thread = _serve(_PrivateHandler)
        _PublicHandler.private_port = int(self.private_server.server_address[1])
        self.public_server, self.public_thread = _serve(_PublicHandler)
        self.addCleanup(self._cleanup)

        self.resolver_calls: list[tuple[str, int]] = []
        self.connector_calls: list[tuple] = []

        def resolver(host: str, port: int, *_args: int) -> list[tuple]:
            self.resolver_calls.append((host, port))
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("8.8.8.8", port),
                )
            ]

        def connector(address_info: tuple, timeout: float) -> socket.socket:
            self.connector_calls.append(address_info)
            return socket.create_connection(
                ("127.0.0.1", int(self.public_server.server_address[1])),
                timeout=timeout,
            )

        self.proxy = GatewayEgressProxy(resolver=resolver, connector=connector)
        launcher.install_gateway_browser_boundary(
            importer=importlib.import_module,
            hermes_root=Path("/opt/hermes"),
            proxy_factory=lambda: self.proxy,
        )

    def _cleanup(self) -> None:
        try:
            browser.cleanup_all_browsers()
        finally:
            launcher._close_gateway_egress_proxy()
            for server, thread in (
                (self.public_server, self.public_thread),
                (self.private_server, self.private_thread),
            ):
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_real_chromium_redirect_never_touches_private_decoy(self) -> None:
        popen_calls = []
        original_popen = browser.subprocess.Popen

        def recording_popen(*args: object, **kwargs: object):
            popen_calls.append((args, kwargs))
            return original_popen(*args, **kwargs)

        with mock.patch.object(browser.subprocess, "Popen", side_effect=recording_popen):
            result = json.loads(
                browser.browser_navigate(
                    "http://8.8.8.8/start",
                    task_id="gateway-egress-runtime",
                )
            )

        self.assertFalse(result["success"], result)
        self.assertEqual(
            _PublicHandler.hits,
            1,
            (result, self.resolver_calls, self.connector_calls, popen_calls),
        )
        self.assertEqual(_PrivateHandler.hits, 0)
        self.assertEqual(self.resolver_calls, [("8.8.8.8", 80)])
        self.assertEqual(len(self.connector_calls), 1)

        agent_call = next(
            (call for call in popen_calls if "agent-browser" in " ".join(call[0][0])),
            None,
        )
        self.assertIsNotNone(agent_call)
        argv = list(agent_call[0][0])
        child_env = agent_call[1]["env"]
        self.assertIn("--session", argv)
        session_name = argv[argv.index("--session") + 1]
        self.assertTrue(session_name.startswith("gw_"), session_name)
        self.assertEqual(child_env["AGENT_BROWSER_PROXY"], self.proxy.proxy_url)
        self.assertEqual(child_env["AGENT_BROWSER_PROXY_BYPASS"], "<-loopback>")
        self.assertTrue(child_env["AGENT_BROWSER_NAMESPACE"].startswith("gw_"))
        self.assertIn("--disable-quic", child_env["AGENT_BROWSER_ARGS"])
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            child_env["AGENT_BROWSER_ARGS"],
        )
        self.assertTrue(
            child_env["AGENT_BROWSER_SOCKET_DIR"].endswith(session_name)
        )
        denied = {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "browser_cdp_url",
            "camofox_url",
            "agent_browser_config",
            "agent_browser_auto_connect",
            "agent_browser_provider",
            "agent_browser_extensions",
            "agent_browser_extension",
            "agent_browser_profile",
            "agent_browser_state",
            "agent_browser_session",
            "agent_browser_session_name",
            "agent_browser_proxy_username",
            "agent_browser_proxy_password",
            "agent_browser_allow_file_access",
        }
        self.assertFalse(denied & {key.lower() for key in child_env})

        chromium_cmdlines = []
        for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                raw = cmdline_path.read_bytes()
            except (OSError, PermissionError):
                continue
            if b"chrom" in raw.lower():
                chromium_cmdlines.append(raw.replace(b"\0", b" ").decode("utf-8", "replace"))
        combined = "\n".join(chromium_cmdlines)
        self.assertIn(f"--proxy-server={self.proxy.proxy_url}", combined)
        self.assertIn("--proxy-bypass-list=<-loopback>", combined)
        self.assertIn("--disable-quic", combined)
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            combined,
        )

        # Killing the mandatory proxy must break navigation; the browser must
        # never silently fall back to a direct connection.
        self.proxy.close()
        second = json.loads(
            browser.browser_navigate(
                "http://8.8.8.8/after-stop",
                task_id="gateway-egress-after-stop",
            )
        )
        self.assertFalse(second["success"], second)
        self.assertEqual(_PublicHandler.hits, 1)
        self.assertEqual(_PrivateHandler.hits, 0)

    def test_telegram_effective_tools_use_builtin_browser_without_code_execution(self) -> None:
        from hermes_cli.config import load_config
        from hermes_cli.plugins import discover_plugins
        from hermes_cli.tools_config import _get_platform_tools
        from model_tools import get_tool_definitions

        discover_plugins(force=True)
        enabled = _get_platform_tools(
            load_config(),
            "telegram",
            include_default_mcp_servers=False,
        )
        self.assertTrue({"browser", "korea", "web"}.issubset(enabled), enabled)
        definitions = get_tool_definitions(
            sorted(enabled),
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = {
            definition["function"]["name"]
            for definition in definitions
        }
        self.assertIn("browser_navigate", names)
        self.assertIn("browser_snapshot", names)
        self.assertNotIn("browser_exec", names)
        self.assertFalse(any(name.startswith("kanban") for name in names))
        self.assertFalse(
            {
                "terminal",
                "execute_code",
                "read_file",
                "write_file",
                "memory_add",
                "skill_manage",
                "yandex_mail_search",
                "yandex_mail_get",
            }
            & names
        )


if __name__ == "__main__":
    unittest.main()
