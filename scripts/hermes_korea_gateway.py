#!/usr/bin/env python3
"""Fail-closed launcher for a Hermes gateway that accepts Telegram locations.

The Korea plugin normally installs its ingress guard during plugin discovery.
Hermes deliberately treats plugin discovery errors as non-fatal, though, which
is the wrong failure mode for exact coordinates. This launcher verifies the
official CLI/bootstrap modules, then installs and asserts the same process-wide
guard before the CLI entry point starts the gateway. If the guard is
unavailable, the gateway never starts.

Hermes applies ``--profile``/sticky-profile selection while importing its CLI
module, before platform modules may cache profile paths.  We therefore import
that official bootstrap first with the final argv, install the guard immediately
after it returns, and only then call the CLI entry point.  Importing the module
does not dispatch or start a gateway.

No credential or coordinate is read, printed, or written here.
"""

from __future__ import annotations

import importlib
import importlib.util
import atexit
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


DEFAULT_PLUGIN_ROOT = Path("/opt/data/plugins")
DEFAULT_HERMES_ROOT = Path("/opt/hermes")
EX_CONFIG = 78
GATEWAY_TERMINAL_SENTINEL = "gateway-restricted"
_BROWSER_BOUNDARY_MARKER = "__hermes_gateway_private_network_guard_v1__"
_BROWSER_EGRESS_MARKER = "__hermes_gateway_mandatory_egress_v1__"
_gateway_egress_proxy: Any = None
EXPECTED_KOREA_TOOLS = frozenset(
    {
        "location_search_context",
        "korea_place_search",
        "korea_geocode",
        "korea_reverse_geocode",
        "korea_route",
        "korea_shopping_search",
        "korea_kakao_links",
    }
)
_FATAL_GUARD_MESSAGE = (
    "FATAL: Telegram location ingress guard is unavailable; "
    "Hermes gateway will not start."
)
_FATAL_RUNTIME_MESSAGE = (
    "FATAL: trusted Hermes runtime is unavailable; gateway will not start."
)
_FATAL_PLUGIN_MESSAGE = (
    "FATAL: exact Korea plugin toolset is unavailable; "
    "Hermes gateway will not start."
)
_FATAL_BROWSER_MESSAGE = (
    "FATAL: gateway browser private-network egress boundary is unavailable; "
    "Hermes gateway will not start."
)


def enforce_same_process_gateway(arguments: Sequence[str]) -> list[str]:
    """Keep ``gateway run`` in this guarded process for its whole lifetime.

    The official s6 image normally redirects a bare ``gateway run`` into a
    separately supervised child.  Python monkey-patches do not survive that
    process boundary.  The external-supervisor flag also makes in-chat restart
    requests exit back to Docker Compose instead of spawning an unguarded
    detached replacement.
    """
    result = list(arguments)
    gateway_index: Optional[int] = None
    index = 0
    while index < len(result):
        argument = result[index]
        if argument in {"--profile", "-p"}:
            index += 2
            continue
        if argument.startswith("--profile="):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        if argument == "gateway":
            gateway_index = index
        break
    if gateway_index is None:
        return result

    tail = result[gateway_index + 1 :]
    if not tail:
        result.append("run")
        tail = ["run"]
    if tail[0] != "run":
        return result
    if "--no-supervise" not in tail:
        result.append("--no-supervise")
    if "--external-supervisor" not in tail:
        result.append("--external-supervisor")
    return result


def _assert_module_origin(module: Any, trusted_root: Path, label: str) -> None:
    """Reject modules that did not load from the immutable Hermes tree."""
    source = getattr(module, "__file__", None)
    if not source:
        raise RuntimeError(f"{label} has no verifiable source path")
    root = Path(trusted_root).resolve()
    candidate = Path(source).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} loaded outside the trusted Hermes tree") from exc


def _load_privacy_module(plugin_root: Path) -> Any:
    """Load only the reviewed privacy module, without a broad sys.path entry."""
    root = Path(plugin_root).resolve()
    source = (root / "korea" / "location_privacy.py").resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Korea privacy module resolves outside plugin root") from exc
    if not source.is_file():
        raise RuntimeError("Korea privacy module is unavailable")

    module_name = "_hermes_korea_location_privacy_guard"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Korea privacy module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def install_required_guard(
    plugin_root: Path = DEFAULT_PLUGIN_ROOT,
    *,
    base_class: Any,
    privacy_loader: Callable[[Path], Any] = _load_privacy_module,
) -> Any:
    """Install and verify the process-lifetime Korea ingress guard."""
    root = Path(plugin_root).resolve()
    if not root.is_dir():
        raise RuntimeError("Hermes plugin directory is unavailable")

    privacy = privacy_loader(root)
    install = getattr(privacy, "install_location_ingress_guard", None)
    is_installed = getattr(
        privacy,
        "is_location_ingress_guard_installed",
        None,
    )
    if not callable(install) or not callable(is_installed):
        raise RuntimeError("Korea ingress guard contract is unavailable")
    if (
        install(base_class=base_class) is not True
        or is_installed(base_class=base_class) is not True
    ):
        raise RuntimeError("Korea ingress guard could not be verified")
    return privacy


def verify_required_toolset(
    *,
    importer: Callable[[str], Any] = importlib.import_module,
    hermes_root: Path = DEFAULT_HERMES_ROOT,
) -> None:
    """Force discovery and require the exact reviewed Korea tool contract."""
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
    if validate_toolset("korea") is not True:
        raise RuntimeError("Korea toolset was not registered")
    resolved = frozenset(resolve_toolset("korea"))
    if resolved != EXPECTED_KOREA_TOOLS:
        raise RuntimeError("Korea toolset does not match the reviewed contract")


def _load_egress_proxy_module() -> Any:
    """Load the reviewed sibling module without making its directory importable."""
    source = Path(__file__).resolve().with_name("gateway_egress_proxy.py")
    if not source.is_file():
        raise RuntimeError("Gateway egress proxy module is unavailable")
    module_name = "_hermes_gateway_egress_proxy"
    existing = sys.modules.get(module_name)
    if existing is not None:
        if Path(getattr(existing, "__file__", "")).resolve() != source:
            raise RuntimeError("Gateway egress proxy module origin changed")
        return existing
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Gateway egress proxy module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _close_gateway_egress_proxy() -> None:
    global _gateway_egress_proxy
    proxy, _gateway_egress_proxy = _gateway_egress_proxy, None
    if proxy is not None:
        try:
            proxy.close()
        except Exception:
            pass


def _start_gateway_egress_proxy(
    proxy_factory: Optional[Callable[[], Any]] = None,
) -> Any:
    """Start once and keep a strong process-lifetime reference."""
    global _gateway_egress_proxy
    if _gateway_egress_proxy is not None:
        if getattr(_gateway_egress_proxy, "is_healthy", False) is not True:
            raise RuntimeError("Existing gateway egress proxy is unhealthy")
        return _gateway_egress_proxy
    if proxy_factory is None:
        module = _load_egress_proxy_module()
        proxy_class = getattr(module, "GatewayEgressProxy", None)
        if not callable(proxy_class):
            raise RuntimeError("Gateway egress proxy contract is unavailable")
        proxy = proxy_class()
    else:
        proxy = proxy_factory()
    proxy_url = getattr(proxy, "proxy_url", None)
    close = getattr(proxy, "close", None)
    if (
        not isinstance(proxy_url, str)
        or not proxy_url.startswith("http://127.0.0.1:")
        or proxy_url.endswith("/")
        or not proxy_url[len("http://127.0.0.1:") :].isdecimal()
        or not callable(close)
        or getattr(proxy, "is_healthy", False) is not True
    ):
        if callable(close):
            close()
        raise RuntimeError("Gateway egress proxy failed its startup contract")
    _gateway_egress_proxy = proxy
    return proxy


def _gateway_browser_args(*, needs_sandbox_bypass: bool) -> str:
    """Return an exact allowlist; late Chromium flags cannot override proxying."""
    arguments = [
        "--disable-quic",
        "--disable-dns-prefetch",
        "--disable-features=AsyncDns",
        "--disable-dev-shm-usage",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if needs_sandbox_bypass:
        arguments.append("--no-sandbox")
    return ",".join(arguments)


def _reviewed_chromium_executable(hermes_root: Path) -> Path:
    """Resolve the single Chromium binary shipped by the pinned base image."""
    root = Path(hermes_root).resolve()
    candidates = tuple(
        root.glob(
            ".playwright/chromium_headless_shell-*/"
            "chrome-headless-shell-linux64/chrome-headless-shell"
        )
    )
    if len(candidates) != 1:
        raise RuntimeError("Pinned Chromium executable is unavailable or ambiguous")
    executable = candidates[0].resolve()
    try:
        executable.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Chromium executable resolves outside trusted runtime") from exc
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("Pinned Chromium executable is not executable")
    return executable


def install_gateway_browser_boundary(
    *,
    importer: Callable[[str], Any] = importlib.import_module,
    hermes_root: Path = DEFAULT_HERMES_ROOT,
    proxy_factory: Optional[Callable[[], Any]] = None,
) -> Any:
    """Install URL guards plus a mandatory connect-time filtering proxy.

    Upstream skips private-network checks when browser and terminal are both
    local, assuming the caller already has terminal access. Telegram does not.
    Importing ``gateway.run`` first executes its config-to-env bridge; the
    gateway-only sentinel remains authoritative in this process.  The proxy
    additionally owns DNS validation and connect, closing redirect/rebinding
    races that a URL pre-check cannot close.
    """
    gateway_module = importer("gateway.run")
    _assert_module_origin(gateway_module, hermes_root, "Hermes gateway runner")
    os.environ["TERMINAL_ENV"] = GATEWAY_TERMINAL_SENTINEL

    # This happens before any browser daemon may be created.  If it later
    # dies, the fixed proxy URL becomes unreachable and navigation fails
    # closed; agent-browser has no DIRECT fallback in the patched child env.
    proxy = _start_gateway_egress_proxy(proxy_factory)

    browser_module = importer("tools.browser_tool")
    _assert_module_origin(browser_module, hermes_root, "Hermes browser tool")
    is_local_backend = getattr(browser_module, "_is_local_backend", None)
    allow_private_urls = getattr(browser_module, "_allow_private_urls", None)
    restrict_browser_evaluate = getattr(
        browser_module,
        "_restrict_browser_evaluate",
        None,
    )
    allow_unsafe_evaluate = getattr(
        browser_module,
        "_allow_unsafe_browser_evaluate",
        None,
    )
    enforce_eval_policy = getattr(
        browser_module,
        "_enforce_browser_eval_policy",
        None,
    )
    evaluate_url_safety = getattr(browser_module, "evaluate_url_safety", None)
    eval_guard_active = getattr(browser_module, "_eval_ssrf_guard_active", None)
    expression_target = getattr(
        browser_module,
        "_expression_targets_private_url",
        None,
    )
    build_browser_env = getattr(browser_module, "_build_browser_env", None)
    needs_sandbox_bypass = getattr(
        browser_module,
        "_needs_chromium_sandbox_bypass",
        None,
    )
    get_cdp_override = getattr(browser_module, "_get_cdp_override_raw", None)
    get_cloud_provider = getattr(browser_module, "_get_cloud_provider", None)
    get_browser_engine = getattr(browser_module, "_get_browser_engine", None)
    is_camofox_mode = getattr(browser_module, "_is_camofox_mode", None)
    use_real_profile = getattr(browser_module, "_use_real_profile", None)
    is_browser_use_mode = getattr(
        browser_module,
        "_is_browser_use_cli_mode",
        None,
    )
    check_browser_requirements = getattr(
        browser_module,
        "check_browser_requirements",
        None,
    )
    active_sessions = getattr(browser_module, "_active_sessions", None)
    if not all(
        callable(item)
        for item in (
            is_local_backend,
            allow_private_urls,
            restrict_browser_evaluate,
            allow_unsafe_evaluate,
            enforce_eval_policy,
            evaluate_url_safety,
            eval_guard_active,
            expression_target,
            build_browser_env,
            needs_sandbox_bypass,
            get_cdp_override,
            get_cloud_provider,
            get_browser_engine,
            is_camofox_mode,
            use_real_profile,
            is_browser_use_mode,
            check_browser_requirements,
        )
    ):
        raise RuntimeError("Hermes browser safety contract is unavailable")
    if not isinstance(active_sessions, dict) or active_sessions:
        raise RuntimeError("A browser session existed before the gateway boundary")
    if (
        get_cdp_override()
        or get_cloud_provider() is not None
        or is_camofox_mode()
        or use_real_profile()
        or is_browser_use_mode()
        or get_browser_engine() != "chrome"
    ):
        raise RuntimeError("Gateway browser is not the reviewed local Chrome backend")

    # Keep the gateway boundary process-lifetime even if another component
    # reapplies terminal config later. Browser functions resolve these module
    # globals at call time; the separate admin CLI runs in another process.
    def gateway_browser_is_not_trusted_local() -> bool:
        return False

    def gateway_browser_denies_private_urls() -> bool:
        return False

    def gateway_browser_restricts_evaluate() -> bool:
        return True

    def gateway_browser_denies_unsafe_evaluate() -> bool:
        return False

    proxy_url = proxy.proxy_url
    chromium_executable = str(_reviewed_chromium_executable(hermes_root))
    forced_args = _gateway_browser_args(
        needs_sandbox_bypass=bool(needs_sandbox_bypass())
    )
    session_namespace = secrets.token_hex(4)

    def gateway_browser_subprocess_env() -> dict[str, str]:
        child_env = build_browser_env()
        if not isinstance(child_env, dict):
            raise RuntimeError("Browser environment builder returned invalid data")
        # Generic proxy variables, local bypass lists, workspace config, and
        # auto-CDP settings must not compete with the reviewed explicit proxy.
        denied_names = {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "agent_browser_proxy",
            "agent_browser_proxy_bypass",
            "agent_browser_args",
            "agent_browser_namespace",
            "agent_browser_config",
            "agent_browser_auto_connect",
            "agent_browser_provider",
            "agent_browser_executable_path",
            "agent_browser_extensions",
            "agent_browser_extension",
            "agent_browser_profile",
            "agent_browser_state",
            "agent_browser_session",
            "agent_browser_session_name",
            "agent_browser_proxy_username",
            "agent_browser_proxy_password",
            "agent_browser_allow_file_access",
            "browser_cdp_url",
            "camofox_url",
        }
        child_env = {
            key: value
            for key, value in child_env.items()
            if key.lower() not in denied_names
        }
        child_env.update(
            {
                "AGENT_BROWSER_PROXY": proxy_url,
                "AGENT_BROWSER_PROXY_BYPASS": "<-loopback>",
                "AGENT_BROWSER_ARGS": forced_args,
                "AGENT_BROWSER_NAMESPACE": f"gw_{session_namespace}",
                "AGENT_BROWSER_EXECUTABLE_PATH": chromium_executable,
            }
        )
        return child_env

    def gateway_create_local_session(
        task_id: str,
        allow_real_profile: bool = True,
    ) -> dict[str, Any]:
        del task_id, allow_real_profile
        return {
            "session_name": (
                f"gw_{session_namespace}_{secrets.token_hex(5)}"
            ),
            "bb_session_id": None,
            "cdp_url": None,
            "features": {"local": True, "gateway_egress": True},
        }

    for boundary_function in (
        gateway_browser_is_not_trusted_local,
        gateway_browser_denies_private_urls,
        gateway_browser_restricts_evaluate,
        gateway_browser_denies_unsafe_evaluate,
    ):
        setattr(boundary_function, _BROWSER_BOUNDARY_MARKER, True)
    for egress_function in (
        gateway_browser_subprocess_env,
        gateway_create_local_session,
    ):
        setattr(egress_function, _BROWSER_EGRESS_MARKER, True)
    setattr(
        browser_module,
        "_is_local_backend",
        gateway_browser_is_not_trusted_local,
    )
    setattr(
        browser_module,
        "_allow_private_urls",
        gateway_browser_denies_private_urls,
    )
    setattr(
        browser_module,
        "_restrict_browser_evaluate",
        gateway_browser_restricts_evaluate,
    )
    setattr(
        browser_module,
        "_allow_unsafe_browser_evaluate",
        gateway_browser_denies_unsafe_evaluate,
    )
    setattr(
        browser_module,
        "_build_browser_env",
        gateway_browser_subprocess_env,
    )
    setattr(
        browser_module,
        "_create_local_session",
        gateway_create_local_session,
    )
    # Tool availability checks run in the parent process before Popen.  Pin
    # only the reviewed executable globally; every other launch setting is
    # still rebuilt from scratch in gateway_browser_subprocess_env().
    os.environ["AGENT_BROWSER_EXECUTABLE_PATH"] = chromium_executable
    is_local_backend = browser_module._is_local_backend
    if is_local_backend() is not False:
        raise RuntimeError("Gateway browser is still treated as trusted local")
    for private_url in (
        "http://127.0.0.1/",
        "http://10.23.45.67/",
        "http://172.20.0.1/",
        "http://192.168.1.1/",
    ):
        decision = evaluate_url_safety(private_url)
        if not isinstance(decision, dict) or decision.get("success") is not False:
            raise RuntimeError("Gateway browser accepted a private-network URL")
    if eval_guard_active("default") is not True:
        raise RuntimeError("Gateway browser eval SSRF guard is inactive")
    if expression_target("fetch('http://127.0.0.1/private')") is None:
        raise RuntimeError("Gateway browser eval URL scanner is unavailable")
    if not enforce_eval_policy("document.cookie"):
        raise RuntimeError("Gateway browser sensitive-eval policy is inactive")
    if check_browser_requirements() is not True:
        raise RuntimeError("Reviewed built-in browser tools are unavailable")
    secure_env = browser_module._build_browser_env()
    if (
        secure_env.get("AGENT_BROWSER_PROXY") != proxy_url
        or secure_env.get("AGENT_BROWSER_PROXY_BYPASS") != "<-loopback>"
        or secure_env.get("AGENT_BROWSER_ARGS") != forced_args
        or secure_env.get("AGENT_BROWSER_NAMESPACE")
        != f"gw_{session_namespace}"
        or secure_env.get("AGENT_BROWSER_EXECUTABLE_PATH")
        != chromium_executable
        or any(
            key.lower()
            in {
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
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
                "browser_cdp_url",
                "camofox_url",
            }
            for key in secure_env
        )
        or getattr(
            browser_module._build_browser_env,
            _BROWSER_EGRESS_MARKER,
            False,
        )
        is not True
        or getattr(
            browser_module._create_local_session,
            _BROWSER_EGRESS_MARKER,
            False,
        )
        is not True
        or not proxy.is_healthy
    ):
        raise RuntimeError("Gateway browser egress contract is inactive")
    return browser_module


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    plugin_root: Path = DEFAULT_PLUGIN_ROOT,
    hermes_root: Path = DEFAULT_HERMES_ROOT,
    importer: Callable[[str], Any] = importlib.import_module,
    privacy_loader: Callable[[Path], Any] = _load_privacy_module,
    browser_boundary: Optional[Callable[[], Any]] = None,
    toolset_verifier: Optional[Callable[[], None]] = None,
) -> int:
    """Bootstrap the profile, verify the boundary, then dispatch Hermes."""
    arguments = enforce_same_process_gateway(
        sys.argv[1:] if argv is None else argv
    )
    previous_argv = sys.argv
    sys.argv = ["hermes", *arguments]
    try:
        try:
            # Importing the official CLI applies profile selection before the
            # official gateway base is imported, because it caches paths.
            cli_module = importer("hermes_cli.main")
            _assert_module_origin(cli_module, hermes_root, "Hermes CLI")
            hermes_main = getattr(cli_module, "main")
            if not callable(hermes_main):
                raise RuntimeError("Hermes CLI entry point is unavailable")

            base_module = importer("gateway.platforms.base")
            _assert_module_origin(
                base_module,
                hermes_root,
                "Hermes gateway base",
            )
            base_class = getattr(base_module, "BasePlatformAdapter")
        except Exception:  # noqa: BLE001 - unavailable CLI cannot start gateway
            print(_FATAL_RUNTIME_MESSAGE, file=sys.stderr)
            return EX_CONFIG

        try:
            if browser_boundary is None:
                install_gateway_browser_boundary(
                    importer=importer,
                    hermes_root=hermes_root,
                )
            else:
                browser_boundary()
        except Exception:  # noqa: BLE001 - fail closed without leaking internals
            print(_FATAL_BROWSER_MESSAGE, file=sys.stderr)
            return EX_CONFIG

        try:
            install_required_guard(
                plugin_root,
                base_class=base_class,
                privacy_loader=privacy_loader,
            )
        except Exception:  # noqa: BLE001 - fail closed without leaking internals
            print(_FATAL_GUARD_MESSAGE, file=sys.stderr)
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
            print(_FATAL_PLUGIN_MESSAGE, file=sys.stderr)
            return EX_CONFIG

        result = hermes_main()
    finally:
        _close_gateway_egress_proxy()
        sys.argv = previous_argv
    return result if isinstance(result, int) and not isinstance(result, bool) else 0


if __name__ == "__main__":
    atexit.register(_close_gateway_egress_proxy)
    raise SystemExit(main())
