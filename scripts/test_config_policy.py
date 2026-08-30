from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_HERMES_PLATFORMS = frozenset(
    {
        "api_server",
        "bluebubbles",
        "cli",
        "cron",
        "dingtalk",
        "discord",
        "email",
        "feishu",
        "homeassistant",
        "matrix",
        "mattermost",
        "qqbot",
        "signal",
        "slack",
        "telegram",
        "webhook",
        "wecom",
        "wecom_callback",
        "weixin",
        "whatsapp",
        "whatsapp_cloud",
        "yuanbao",
    }
)
CUSTOM_PLUGIN_TOOLSETS = {"korea", "yandex_mail"}


def _list_mapping(section: str) -> dict[str, list[str]]:
    """Read one simple top-level YAML mapping of scalar lists without deps."""
    lines = (ROOT / "config.yaml").read_text("utf-8").splitlines()
    marker = f"{section}:"
    try:
        start = lines.index(marker) + 1
    except ValueError as exc:
        raise AssertionError(f"missing config section: {section}") from exc

    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start:]:
        if line and not line.startswith((" ", "#")):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            current = stripped[:-1]
            result[current] = []
            continue
        if line.startswith("    - ") and current is not None:
            result[current].append(stripped[2:].strip())
    return result


class ConfigPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform_toolsets = _list_mapping("platform_toolsets")
        cls.known_plugin_toolsets = _list_mapping("known_plugin_toolsets")

    def test_every_pinned_platform_knows_both_custom_plugins(self) -> None:
        known = self.known_plugin_toolsets
        self.assertEqual(set(known), PINNED_HERMES_PLATFORMS)
        for platform in PINNED_HERMES_PLATFORMS:
            self.assertEqual(set(known[platform]), CUSTOM_PLUGIN_TOOLSETS)

    def test_yandex_mail_is_not_enabled_on_any_platform(self) -> None:
        configured = self.platform_toolsets
        self.assertNotIn(
            "yandex_mail",
            {
                str(toolset)
                for toolsets in configured.values()
                for toolset in toolsets
            },
        )

    def test_korea_is_enabled_only_for_cli_and_telegram(self) -> None:
        configured = self.platform_toolsets
        enabled = {
            platform
            for platform, toolsets in configured.items()
            if "korea" in toolsets
        }
        self.assertEqual(enabled, {"cli", "telegram"})

    def test_browser_private_network_and_eval_policy_is_explicit(self) -> None:
        config_text = (ROOT / "config.yaml").read_text("utf-8")
        self.assertIn("  backend: off", config_text)
        self.assertIn("  allow_private_urls: false", config_text)
        self.assertIn("  auto_local_for_private_urls: false", config_text)
        self.assertIn("  restrict_evaluate: true", config_text)
        self.assertIn("  allow_unsafe_evaluate: false", config_text)

    def test_single_gateway_has_one_external_supervisor(self) -> None:
        compose_text = (ROOT / "docker-compose.yml").read_text("utf-8")
        dockerfile_text = (ROOT / "Dockerfile").read_text("utf-8")
        init_text = (
            ROOT / "scripts" / "hermes_single_gateway_init.sh"
        ).read_text("utf-8")
        self.assertIn('HERMES_GATEWAY_NO_SUPERVISE: "1"', compose_text)
        self.assertIn("stop_grace_period: 60s", compose_text)
        self.assertIn(
            "COPY scripts/hermes_single_gateway_init.sh "
            "/etc/cont-init.d/02-reconcile-profiles",
            dockerfile_text,
        )
        self.assertIn("single guarded gateway", init_text)


if __name__ == "__main__":
    unittest.main()
