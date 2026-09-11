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

    def test_personal_surfaces_have_full_first_party_tools_and_plugins(self) -> None:
        configured = self.platform_toolsets
        self.assertEqual(
            configured["telegram"],
            ["hermes-telegram", "korea", "yandex_mail", "no_mcp"],
        )
        self.assertEqual(
            configured["cli"],
            ["hermes-cli", "korea", "yandex_mail"],
        )
        self.assertEqual(
            configured["cron"],
            ["hermes-cron", "korea", "yandex_mail", "no_mcp"],
        )

    def test_agent_writes_are_enabled_but_secrets_stay_read_only(self) -> None:
        config_text = (ROOT / "config.yaml").read_text("utf-8")
        compose_text = (ROOT / "docker-compose.yml").read_text("utf-8")
        self.assertIn(
            "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n"
            "  write_approval: false",
            config_text,
        )
        self.assertIn(
            "  guard_agent_created: true\n  write_approval: false",
            config_text,
        )
        for mount in (
            "./config.yaml:/opt/data/config.yaml",
            "./SOUL.md:/opt/data/SOUL.md",
            "./skills:/opt/data/custom-skills",
            "./plugins:/opt/data/plugins",
        ):
            self.assertIn(f"- {mount}", compose_text)
            self.assertNotIn(f"- {mount}:ro", compose_text)
        self.assertIn("source: ./.env", compose_text)
        self.assertIn("target: /opt/data/.env", compose_text)
        self.assertIn("read_only: true", compose_text)
        self.assertGreaterEqual(compose_text.count("create_host_path: false"), 2)
        self.assertIn("- ./credentials:/credentials:ro", compose_text)

    def test_browser_private_network_and_eval_policy_is_explicit(self) -> None:
        config_text = (ROOT / "config.yaml").read_text("utf-8")
        self.assertIn("  backend: off", config_text)
        self.assertIn("  cloud_provider: local", config_text)
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

    def test_always_on_memory_ownership_is_repaired_at_boot(self) -> None:
        dockerfile_text = (ROOT / "Dockerfile").read_text("utf-8")
        init_text = (
            ROOT / "scripts" / "hermes_writable_state_init.sh"
        ).read_text("utf-8")
        self.assertIn(
            "COPY scripts/hermes_writable_state_init.sh "
            "/etc/cont-init.d/014-hermes-writable-state",
            dockerfile_text,
        )
        self.assertIn("chown -R", init_text)
        self.assertIn("/opt/data/memories", init_text)


if __name__ == "__main__":
    unittest.main()
