"""Tests for the stdlib-only Yandex Mail OAuth implementation."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import auth
import setup as setup_cli


CLIENT_ID = "public-client-id"
CLIENT_SECRET = "super-secret-client-value"
USERNAME = "reader@example.com"
ACCESS_TOKEN = "super-secret-access-token"
REFRESH_TOKEN = "super-secret-refresh-token"


class JsonResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "JsonResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


class OAuthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.credentials_path = self.root / "credentials.json"
        self.token_path = self.root / "state" / "oauth.json"
        self.credentials_path.write_text(
            json.dumps(
                {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "username": USERNAME,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_token(
        self,
        *,
        access_token: str = ACCESS_TOKEN,
        refresh_token: Optional[str] = REFRESH_TOKEN,
        expires_at: Optional[float] = None,
        scope: str = auth.REQUIRED_SCOPE,
        username: str = USERNAME,
    ) -> Dict[str, Any]:
        token: Dict[str, Any] = {
            "version": 1,
            "username": username,
            "token_type": "bearer",
            "access_token": access_token,
            "scope": scope,
            "expires_at": expires_at if expires_at is not None else time.time() + 3600,
            "obtained_at": time.time(),
        }
        if refresh_token is not None:
            token["refresh_token"] = refresh_token
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token), encoding="utf-8")
        return token

    def test_authorization_url_has_exact_readonly_scope_and_manual_redirect(self) -> None:
        url = auth.build_authorization_url(self.credentials_path)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)

        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", auth.AUTHORIZE_URL)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [auth.REDIRECT_URI])
        self.assertEqual(query["scope"], ["mail:imap_ro"])
        self.assertEqual(query["force_confirm"], ["yes"])
        self.assertNotIn("mail:imap_full", url)
        self.assertNotIn("mail:smtp", url)
        self.assertNotIn(CLIENT_SECRET, url)

    def test_extract_code_accepts_bare_code_and_callback_url(self) -> None:
        self.assertEqual(auth.extract_authorization_code(" abc123 "), "abc123")
        callback = f"{auth.REDIRECT_URI}?code=abc%2F123"
        self.assertEqual(auth.extract_authorization_code(callback), "abc/123")

    def test_extract_code_rejects_foreign_and_error_callbacks_without_details(self) -> None:
        with self.assertRaises(auth.OAuthRequestError):
            auth.extract_authorization_code("https://evil.example/?code=abc")
        with self.assertRaises(auth.OAuthRequestError) as raised:
            auth.extract_authorization_code(
                f"{auth.REDIRECT_URI}?error=denied&error_description={ACCESS_TOKEN}"
            )
        self.assertNotIn(ACCESS_TOKEN, str(raised.exception))

    def test_exchange_posts_expected_form_and_writes_private_token(self) -> None:
        captured: Dict[str, Any] = {}

        def opener(request: Any, timeout: float) -> JsonResponse:
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["form"] = parse_qs(request.data.decode("ascii"))
            captured["timeout"] = timeout
            return JsonResponse(
                {
                    "token_type": "bearer",
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": REFRESH_TOKEN,
                    "expires_in": 3600,
                    # Yandex may omit scope when it equals the requested set.
                }
            )

        auth.exchange_authorization_code(
            "authorization-code",
            self.credentials_path,
            self.token_path,
            opener=opener,
        )

        self.assertEqual(captured["url"], auth.TOKEN_URL)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["form"],
            {
                "grant_type": ["authorization_code"],
                "code": ["authorization-code"],
                "client_id": [CLIENT_ID],
                "client_secret": [CLIENT_SECRET],
            },
        )
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["scope"], auth.REQUIRED_SCOPE)
        self.assertEqual(saved["username"], USERNAME)
        self.assertEqual(saved["access_token"], ACCESS_TOKEN)
        if os.name == "posix":
            mode = stat.S_IMODE(self.token_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            directory_mode = stat.S_IMODE(self.token_path.parent.stat().st_mode)
            self.assertEqual(directory_mode, 0o700)
        temporary_files = list(self.token_path.parent.glob(".oauth.json.*.tmp"))
        self.assertEqual(temporary_files, [])

    def test_exchange_rejects_full_or_smtp_scope_without_replacing_token(self) -> None:
        self._write_token(access_token="old-token")
        original = self.token_path.read_bytes()

        for unsafe_scope in (
            "mail:imap_ro mail:imap_full",
            "mail:imap_ro mail:smtp",
            "mail:imap_ro login:email",
            "mail:imap_ro disk:all",
        ):
            with self.subTest(scope=unsafe_scope):
                with self.assertRaises(auth.OAuthRequestError):
                    auth.exchange_authorization_code(
                        "code",
                        self.credentials_path,
                        self.token_path,
                        opener=lambda *_args, **_kwargs: JsonResponse(
                            {
                                "token_type": "bearer",
                                "access_token": "new-token",
                                "refresh_token": "new-refresh",
                                "expires_in": 3600,
                                "scope": unsafe_scope,
                            }
                        ),
                    )
                self.assertEqual(self.token_path.read_bytes(), original)

    def test_valid_saved_token_is_returned_without_network(self) -> None:
        self._write_token()

        username, token = auth.get_access_token(
            self.credentials_path,
            self.token_path,
            opener=lambda *_args, **_kwargs: self.fail("network should not be used"),
        )

        self.assertEqual((username, token), (USERNAME, ACCESS_TOKEN))

    def test_expired_token_refreshes_and_rotates_both_tokens(self) -> None:
        self._write_token(expires_at=time.time() - 1)
        captured: Dict[str, Any] = {}

        def opener(request: Any, timeout: float) -> JsonResponse:
            captured.update(parse_qs(request.data.decode("ascii")))
            return JsonResponse(
                {
                    "token_type": "bearer",
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 7200,
                    "scope": "mail:imap_ro",
                }
            )

        result = auth.get_access_token(
            self.credentials_path,
            self.token_path,
            opener=opener,
        )

        self.assertEqual(result, (USERNAME, "rotated-access"))
        self.assertEqual(captured["grant_type"], ["refresh_token"])
        self.assertEqual(captured["refresh_token"], [REFRESH_TOKEN])
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "rotated-refresh")

    def test_refresh_preserves_refresh_token_and_effective_scope_when_omitted(self) -> None:
        self._write_token(expires_at=time.time() - 1)

        result = auth.get_access_token(
            self.credentials_path,
            self.token_path,
            opener=lambda *_args, **_kwargs: JsonResponse(
                {
                    "token_type": "bearer",
                    "access_token": "new-access",
                    "expires_in": 3600,
                }
            ),
        )

        self.assertEqual(result, (USERNAME, "new-access"))
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], REFRESH_TOKEN)
        self.assertEqual(saved["scope"], auth.REQUIRED_SCOPE)

    def test_concurrent_refresh_only_calls_yandex_once(self) -> None:
        self._write_token(expires_at=time.time() - 1)
        calls = 0
        calls_guard = threading.Lock()
        start = threading.Barrier(3)
        results = []
        failures = []

        def opener(_request: Any, timeout: float) -> JsonResponse:
            nonlocal calls
            with calls_guard:
                calls += 1
            time.sleep(0.05)
            return JsonResponse(
                {
                    "token_type": "bearer",
                    "access_token": "shared-new-access",
                    "refresh_token": "shared-new-refresh",
                    "expires_in": 3600,
                    "scope": auth.REQUIRED_SCOPE,
                }
            )

        def worker() -> None:
            try:
                start.wait(timeout=2)
                results.append(
                    auth.get_access_token(
                        self.credentials_path,
                        self.token_path,
                        opener=opener,
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(failures, [])
        self.assertEqual(calls, 1)
        self.assertEqual(
            results,
            [(USERNAME, "shared-new-access"), (USERNAME, "shared-new-access")],
        )

    def test_saved_token_scope_and_mailbox_binding_are_checked(self) -> None:
        for scope in (
            "mail:imap_full",
            "mail:smtp",
            "login:email",
            "mail:imap_ro login:email",
        ):
            with self.subTest(scope=scope):
                self._write_token(scope=scope)
                with self.assertRaises(auth.TokenError):
                    auth.get_access_token(self.credentials_path, self.token_path)

        self._write_token(username="different@example.com")
        with self.assertRaises(auth.TokenError):
            auth.get_access_token(self.credentials_path, self.token_path)

    def test_non_finite_lifetimes_are_rejected(self) -> None:
        for invalid_lifetime in ("nan", "inf", "-inf"):
            with self.subTest(expires_in=invalid_lifetime):
                with self.assertRaises(auth.OAuthRequestError):
                    auth.exchange_authorization_code(
                        "code",
                        self.credentials_path,
                        self.token_path,
                        opener=lambda *_args, value=invalid_lifetime, **_kwargs: JsonResponse(
                            {
                                "token_type": "bearer",
                                "access_token": ACCESS_TOKEN,
                                "refresh_token": REFRESH_TOKEN,
                                "expires_in": value,
                                "scope": auth.REQUIRED_SCOPE,
                            }
                        ),
                    )

        self._write_token(expires_at=float("nan"))
        with self.assertRaises(auth.TokenError):
            auth.get_access_token(self.credentials_path, self.token_path)

        self._write_token()
        for invalid_window in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(min_validity_seconds=invalid_window):
                with self.assertRaises(auth.TokenError):
                    auth.get_access_token(
                        self.credentials_path,
                        self.token_path,
                        min_validity_seconds=invalid_window,
                    )

    def test_expired_token_without_refresh_requires_authorization(self) -> None:
        self._write_token(refresh_token=None, expires_at=time.time() - 1)
        with self.assertRaises(auth.TokenError):
            auth.get_access_token(self.credentials_path, self.token_path)

    def test_http_and_transport_errors_never_expose_secrets(self) -> None:
        error = HTTPError(
            auth.TOKEN_URL,
            400,
            f"server echoed {CLIENT_SECRET} {ACCESS_TOKEN}",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps({"error_description": ACCESS_TOKEN}).encode("utf-8")
            ),
        )
        with self.assertRaises(auth.OAuthRequestError) as raised:
            auth.exchange_authorization_code(
                "secret-code",
                self.credentials_path,
                self.token_path,
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
            )
        rendered = str(raised.exception)
        self.assertNotIn(CLIENT_SECRET, rendered)
        self.assertNotIn(ACCESS_TOKEN, rendered)
        self.assertNotIn("secret-code", rendered)

        with self.assertRaises(auth.OAuthRequestError) as raised_runtime:
            auth.exchange_authorization_code(
                "secret-code",
                self.credentials_path,
                self.token_path,
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError(f"transport leaked {CLIENT_SECRET}")
                ),
            )
        self.assertNotIn(CLIENT_SECRET, str(raised_runtime.exception))

    def test_bad_credentials_and_token_json_do_not_echo_file_contents(self) -> None:
        self.credentials_path.write_text(CLIENT_SECRET, encoding="utf-8")
        with self.assertRaises(auth.CredentialsError) as bad_credentials:
            auth.load_credentials(self.credentials_path)
        self.assertNotIn(CLIENT_SECRET, str(bad_credentials.exception))

        self.credentials_path.write_text(
            json.dumps(
                {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "username": USERNAME,
                }
            ),
            encoding="utf-8",
        )
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(ACCESS_TOKEN, encoding="utf-8")
        with self.assertRaises(auth.TokenError) as bad_token:
            auth.get_access_token(self.credentials_path, self.token_path)
        self.assertNotIn(ACCESS_TOKEN, str(bad_token.exception))

    def test_status_contains_no_credentials_or_tokens(self) -> None:
        self._write_token()
        status_value = auth.get_status(self.credentials_path, self.token_path)
        rendered = json.dumps(status_value)
        for secret in (CLIENT_ID, CLIENT_SECRET, USERNAME, ACCESS_TOKEN, REFRESH_TOKEN):
            self.assertNotIn(secret, rendered)
        self.assertTrue(status_value["authorized"])
        self.assertEqual(status_value["scope"], auth.REQUIRED_SCOPE)

    def test_check_live_uses_xoauth2_and_readonly_inbox(self) -> None:
        client = Mock()
        client.select.return_value = ("OK", [b"2"])
        captured: Dict[str, Any] = {}

        def authenticate(mechanism: str, callback: Any) -> None:
            captured["mechanism"] = mechanism
            captured["payload"] = callback(b"")

        client.authenticate.side_effect = authenticate
        with patch.object(
            auth,
            "get_access_token",
            return_value=(USERNAME, ACCESS_TOKEN),
        ), patch.object(auth.imaplib, "IMAP4_SSL", return_value=client) as factory:
            self.assertTrue(auth.check_live(self.credentials_path, self.token_path))

        factory.assert_called_once()
        factory_args = factory.call_args
        self.assertEqual(factory_args.args, (auth.IMAP_HOST, auth.IMAP_PORT))
        self.assertEqual(factory_args.kwargs["timeout"], 20.0)
        tls_context = factory_args.kwargs["ssl_context"]
        self.assertTrue(tls_context.check_hostname)
        self.assertEqual(tls_context.verify_mode, auth.ssl.CERT_REQUIRED)
        self.assertEqual(captured["mechanism"], "XOAUTH2")
        self.assertEqual(
            captured["payload"],
            f"user={USERNAME}\x01auth=Bearer {ACCESS_TOKEN}\x01\x01".encode(),
        )
        client.select.assert_called_once_with("INBOX", readonly=True)
        client.logout.assert_called_once()

    def test_setup_cli_outputs_are_secret_free(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        return_code = setup_cli.main(
            ["auth-url", "--credentials", str(self.credentials_path)],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(return_code, 0)
        self.assertIn("scope=mail%3Aimap_ro", output.getvalue())
        self.assertNotIn(CLIENT_SECRET, output.getvalue())

        self._write_token()
        output = io.StringIO()
        return_code = setup_cli.main(
            [
                "status",
                "--credentials",
                str(self.credentials_path),
                "--tokens",
                str(self.token_path),
            ],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(return_code, 0)
        rendered = output.getvalue()
        for secret in (CLIENT_SECRET, USERNAME, ACCESS_TOKEN, REFRESH_TOKEN):
            self.assertNotIn(secret, rendered)

    def test_setup_auth_code_does_not_print_code_or_tokens(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        callback = f"{auth.REDIRECT_URI}?code=private-code"
        with patch.object(
            setup_cli.getpass,
            "getpass",
            return_value=callback,
        ) as prompt, patch.object(
            setup_cli.yandex_auth,
            "exchange_authorization_code",
        ) as exchange:
            result = setup_cli.main(
                [
                    "auth-code",
                    "--credentials",
                    str(self.credentials_path),
                    "--tokens",
                    str(self.token_path),
                ],
                stdout=output,
                stderr=errors,
            )

        self.assertEqual(result, 0)
        prompt.assert_called_once_with(
            "Yandex authorization code: ",
            stream=errors,
        )
        exchange.assert_called_once_with(
            callback,
            str(self.credentials_path),
            str(self.token_path),
        )
        self.assertNotIn("private-code", output.getvalue())
        self.assertNotIn(ACCESS_TOKEN, output.getvalue())


if __name__ == "__main__":
    unittest.main()
