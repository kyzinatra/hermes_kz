from __future__ import annotations

import json
import os
import unittest
from unittest import mock


try:
    from tools import browser_tool as browser
except ImportError:  # Host-only unit runs do not include the Hermes runtime.
    browser = None


@unittest.skipIf(browser is None, "requires the pinned Hermes runtime")
class GatewayBrowserPolicyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prior_terminal_env = os.environ.get("TERMINAL_ENV")
        os.environ["TERMINAL_ENV"] = "gateway-restricted"

    def tearDown(self) -> None:
        if self.prior_terminal_env is None:
            os.environ.pop("TERMINAL_ENV", None)
        else:
            os.environ["TERMINAL_ENV"] = self.prior_terminal_env

    def test_private_urls_block_and_public_url_passes(self) -> None:
        with mock.patch.object(browser, "check_website_access", return_value=None), mock.patch.object(
            browser,
            "_allow_private_urls",
            return_value=False,
        ):
            self.assertFalse(browser._is_local_backend())
            for url in (
                "http://127.0.0.1/",
                "http://10.23.45.67/",
                "http://172.20.0.1/",
                "http://192.168.1.1/",
            ):
                decision = browser.evaluate_url_safety(url)
                self.assertIsInstance(decision, dict)
                self.assertFalse(decision["success"])
            self.assertIsNone(browser.evaluate_url_safety("https://example.com/"))

    def test_redirect_to_private_is_blankened_and_blocked(self) -> None:
        calls = []

        def browser_command(_task_id, command, args, **_kwargs):
            calls.append((command, list(args)))
            if command == "open" and args == ["https://example.com/start"]:
                return {
                    "success": True,
                    "data": {
                        "title": "redirected",
                        "url": "http://127.0.0.1/private",
                    },
                }
            if command == "open" and args == ["about:blank"]:
                return {"success": True, "data": {"url": "about:blank"}}
            self.fail(f"unexpected browser command: {command} {args}")

        with mock.patch.object(browser, "_run_browser_command", side_effect=browser_command), mock.patch.object(
            browser,
            "_get_session_info",
            return_value={"_first_nav": False},
        ), mock.patch.object(
            browser,
            "_allow_private_urls",
            return_value=False,
        ), mock.patch.object(
            browser,
            "check_website_access",
            return_value=None,
        ):
            result = json.loads(
                browser.browser_navigate(
                    "https://example.com/start",
                    task_id="ssrf-audit",
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("redirect", result["error"].lower())
        self.assertIn(("open", ["about:blank"]), calls)

    def test_javascript_fetch_to_private_is_blocked_before_execution(self) -> None:
        with mock.patch.object(
            browser,
            "_allow_private_urls",
            return_value=False,
        ), mock.patch.object(
            browser,
            "_is_local_sidecar_key",
            return_value=False,
        ), mock.patch.object(
            browser,
            "_run_browser_command",
            side_effect=AssertionError("private fetch must not execute"),
        ):
            result = json.loads(
                browser._browser_eval(
                    "fetch('http://127.0.0.1/private')",
                    task_id="ssrf-audit",
                )
            )
        self.assertFalse(result["success"])
        self.assertIn("private", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
