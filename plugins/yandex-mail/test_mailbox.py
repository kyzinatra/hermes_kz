"""Unit tests for the dependency-free read-only Yandex Mail client."""

from __future__ import annotations

from email.message import EmailMessage
from email import policy
import importlib.util
import imaplib
import json
import os
from pathlib import Path
import ssl
import sys
import types
import unittest
from unittest.mock import patch


_MODULE_PATH = Path(__file__).with_name("mailbox.py")
_SPEC = importlib.util.spec_from_file_location(
    "yandex_mail_readonly_mailbox_under_test",
    _MODULE_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_MAILBOX = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MAILBOX
_SPEC.loader.exec_module(_MAILBOX)

YandexReadonlyMailbox = _MAILBOX.YandexReadonlyMailbox
SECURITY_NOTICE = _MAILBOX.SECURITY_NOTICE


def _header_block(raw_message: bytes) -> bytes:
    separator = b"\r\n\r\n" if b"\r\n\r\n" in raw_message else b"\n\n"
    return raw_message.split(separator, 1)[0] + separator


def _plain_message(
    *,
    subject: str = "Subject",
    body: str = "Hello from Yandex Mail.",
) -> bytes:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "reader@yandex.ru"
    message["Subject"] = subject
    message["Date"] = "Sat, 08 Aug 2026 12:00:00 +0300"
    message["Message-ID"] = "<message-1@example.com>"
    message.set_content(body)
    return message.as_bytes(policy=policy.SMTP)


class FakeIMAP:
    def __init__(
        self,
        *,
        search_uids: bytes = b"1",
        uidvalidity: bytes | None = b"777",
        auth_error: Exception | None = None,
        search_status: str = "OK",
    ) -> None:
        self.search_uids = search_uids
        self.uidvalidity = uidvalidity
        self.auth_error = auth_error
        self.search_status = search_status
        self.headers: dict[str, tuple[bytes, int | None, str]] = {}
        self.messages: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.auth_payload: bytes | None = None
        self.logout_called = False

    def add_message(
        self,
        uid: str,
        raw_message: bytes,
        *,
        size: int | None = None,
        flags: str = "",
    ) -> None:
        if size is None:
            size = len(raw_message)
        self.headers[uid] = (_header_block(raw_message), size, flags)
        self.messages[uid] = raw_message

    def authenticate(self, mechanism, callback):
        self.calls.append(("AUTHENTICATE", mechanism))
        self.auth_payload = callback(b"")
        if self.auth_error is not None:
            raise self.auth_error
        return "OK", [b"authenticated"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, kind):
        self.calls.append(("RESPONSE", kind))
        values = None if self.uidvalidity is None else [self.uidvalidity]
        return kind, values

    def uid(self, command, *args):
        self.calls.append(("UID", command, *args))
        if command == "SEARCH":
            return self.search_status, [self.search_uids]
        if command != "FETCH":
            raise AssertionError(f"Unexpected UID command: {command}")

        uid, query = args
        if "HEADER.FIELDS" in query:
            record = self.headers.get(uid)
            if record is None:
                return "OK", [None]
            header, size, flags = record
            size_fragment = f" RFC822.SIZE {size}" if size is not None else ""
            metadata = (
                f"1 (UID {uid}{size_fragment} FLAGS ({flags}) "
                f"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] "
                f"{{{len(header)}}}"
            ).encode("ascii")
            return "OK", [(metadata, header), b")"]

        if query == "(BODY.PEEK[])":
            raw_message = self.messages.get(uid)
            if raw_message is None:
                return "OK", [None]
            metadata = (
                f"1 (UID {uid} BODY[] {{{len(raw_message)}}}"
            ).encode("ascii")
            return "OK", [(metadata, raw_message), b")"]

        raise AssertionError(f"Unexpected FETCH query: {query}")

    def logout(self):
        self.calls.append(("LOGOUT",))
        self.logout_called = True
        return "BYE", [b"logout"]


class RecordingFactory:
    def __init__(self, client: FakeIMAP) -> None:
        self.client = client
        self.calls: list[tuple] = []

    def __call__(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        return self.client


def _client(
    fake: FakeIMAP,
    *,
    token_provider=None,
    body_char_limit: int = 20_000,
    max_message_bytes: int = 2_000_000,
    use_default_ssl_context: bool = False,
):
    factory = RecordingFactory(fake)
    kwargs = {}
    if not use_default_ssl_context:
        kwargs["ssl_context_factory"] = lambda: "tls-context"
    mailbox = YandexReadonlyMailbox(
        token_provider=token_provider
        or (lambda: ("reader@yandex.ru", "oauth-secret-token")),
        body_char_limit=body_char_limit,
        max_message_bytes=max_message_bytes,
        imap_factory=factory,
        **kwargs,
    )
    return mailbox, factory


class YandexReadonlyMailboxTests(unittest.TestCase):
    def test_plugin_ignores_imap_endpoint_environment_overrides(self):
        plugin_dir = Path(__file__).parent
        package_name = "_yandex_mail_plugin_endpoint_test"
        spec = importlib.util.spec_from_file_location(
            package_name,
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        assert spec is not None and spec.loader is not None

        auth_stub = types.ModuleType(f"{package_name}.auth")
        auth_stub.get_access_token = lambda: (
            "reader@yandex.ru",
            "contract-test-token",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        sys.modules[f"{package_name}.auth"] = auth_stub

        try:
            spec.loader.exec_module(module)
            with patch.dict(
                os.environ,
                {
                    "YANDEX_MAIL_IMAP_HOST": "attacker.example",
                    "YANDEX_MAIL_IMAP_PORT": "1",
                },
            ):
                mailbox = module._build_mailbox()

            self.assertEqual(mailbox._host, module.DEFAULT_IMAP_HOST)
            self.assertEqual(mailbox._port, module.DEFAULT_IMAP_PORT)
            self.assertEqual((mailbox._host, mailbox._port), ("imap.ya.ru", 993))
        finally:
            for loaded_name in list(sys.modules):
                if loaded_name == package_name or loaded_name.startswith(
                    f"{package_name}."
                ):
                    sys.modules.pop(loaded_name, None)

    def test_hermes_style_plugin_load_registers_exactly_two_json_tools(self):
        plugin_dir = Path(__file__).parent
        package_name = "_yandex_mail_plugin_contract_test"
        init_path = plugin_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            package_name,
            init_path,
            submodule_search_locations=[str(plugin_dir)],
        )
        assert spec is not None and spec.loader is not None

        auth_stub = types.ModuleType(f"{package_name}.auth")
        auth_stub.get_access_token = lambda: (
            "reader@yandex.ru",
            "contract-test-token",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        sys.modules[f"{package_name}.auth"] = auth_stub

        class Context:
            def __init__(self):
                self.tools = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

        class StubMailbox:
            def list_inbox(self, **kwargs):
                return {
                    "success": True,
                    "security_notice": SECURITY_NOTICE,
                    "operation": "list",
                    "args": kwargs,
                }

            def read_message(self, uid, **kwargs):
                return {
                    "success": True,
                    "security_notice": SECURITY_NOTICE,
                    "operation": "read",
                    "uid": uid,
                    "args": kwargs,
                }

        try:
            spec.loader.exec_module(module)
            module._build_mailbox = StubMailbox
            context = Context()
            module.register(context)

            self.assertEqual(len(context.tools), 2)
            self.assertEqual(
                {tool["name"] for tool in context.tools},
                {"yandex_mail_list_inbox", "yandex_mail_read_message"},
            )
            self.assertEqual(
                {tool["toolset"] for tool in context.tools},
                {"yandex_mail"},
            )
            for tool in context.tools:
                self.assertEqual(tool["schema"]["name"], tool["name"])
                self.assertTrue(callable(tool["handler"]))

            handlers = {tool["name"]: tool["handler"] for tool in context.tools}
            listed = json.loads(
                handlers["yandex_mail_list_inbox"](
                    {"limit": 3, "unread_only": True, "before_uid": "100"}
                )
            )
            read = json.loads(
                handlers["yandex_mail_read_message"](
                    {"uid": "42", "uidvalidity": "777"}
                )
            )
            self.assertEqual(listed["operation"], "list")
            self.assertEqual(
                listed["args"],
                {"limit": 3, "unread_only": True, "before_uid": "100"},
            )
            self.assertEqual(read["operation"], "read")
            self.assertEqual(read["uid"], "42")
            self.assertEqual(read["args"]["expected_uidvalidity"], "777")
            self.assertEqual(listed["security_notice"], SECURITY_NOTICE)
            self.assertEqual(read["security_notice"], SECURITY_NOTICE)
        finally:
            for loaded_name in list(sys.modules):
                if loaded_name == package_name or loaded_name.startswith(
                    f"{package_name}."
                ):
                    sys.modules.pop(loaded_name, None)

    def test_xoauth2_tls_readonly_inbox_and_body_peek_only(self):
        fake = FakeIMAP(search_uids=b"42")
        fake.add_message("42", _plain_message())
        mailbox, factory = _client(fake, use_default_ssl_context=True)

        result = mailbox.read_message("42", expected_uidvalidity="777")

        self.assertTrue(result["success"])
        self.assertEqual(factory.calls[0][0:2], ("imap.ya.ru", 993))
        self.assertEqual(factory.calls[0][2]["timeout"], 30.0)
        tls_context = factory.calls[0][2]["ssl_context"]
        self.assertIsInstance(tls_context, ssl.SSLContext)
        self.assertTrue(tls_context.check_hostname)
        self.assertEqual(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(
            fake.auth_payload,
            b"user=reader@yandex.ru\x01auth=Bearer oauth-secret-token\x01\x01",
        )
        self.assertIn(("SELECT", "INBOX", True), fake.calls)
        fetch_queries = [
            call[-1]
            for call in fake.calls
            if call[:2] == ("UID", "FETCH")
        ]
        self.assertEqual(len(fetch_queries), 2)
        self.assertTrue(all("BODY.PEEK" in query for query in fetch_queries))
        serialized_calls = repr(fake.calls).upper()
        for forbidden in ("STORE", "COPY", "MOVE", "EXPUNGE", "APPEND"):
            self.assertNotIn(forbidden, serialized_calls)
        self.assertTrue(fake.logout_called)

    def test_list_uses_stable_uids_newest_first_and_honors_limit(self):
        fake = FakeIMAP(search_uids=b"2 10 3 10")
        for uid in ("2", "3", "10"):
            fake.add_message(uid, _plain_message(subject=f"Message {uid}"))
        mailbox, _factory = _client(fake)

        result = mailbox.list_inbox(limit=2)

        self.assertTrue(result["success"])
        self.assertEqual([item["uid"] for item in result["messages"]], ["10", "3"])
        self.assertEqual(result["uidvalidity"], "777")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_before_uid"], "3")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["mailbox"], "INBOX")

        next_page = mailbox.list_inbox(limit=2, before_uid=result["next_before_uid"])
        self.assertTrue(next_page["success"])
        self.assertEqual(
            [item["uid"] for item in next_page["messages"]],
            ["2"],
        )
        self.assertFalse(next_page["has_more"])
        self.assertIsNone(next_page["next_before_uid"])

    def test_unread_filter_uses_uid_search_unseen(self):
        fake = FakeIMAP(search_uids=b"7")
        fake.add_message("7", _plain_message(), flags="")
        mailbox, _factory = _client(fake)

        result = mailbox.list_inbox(unread_only=True)

        self.assertTrue(result["success"])
        self.assertIn(("UID", "SEARCH", None, "UNSEEN"), fake.calls)
        self.assertTrue(result["messages"][0]["unread"])

    def test_seen_flag_and_encoded_russian_headers_are_decoded(self):
        fake = FakeIMAP(search_uids=b"8")
        raw_message = _plain_message(subject="Тестовое письмо")
        fake.add_message("8", raw_message, flags="\\Seen \\Answered")
        mailbox, _factory = _client(fake)

        result = mailbox.list_inbox()

        envelope = result["messages"][0]
        self.assertEqual(envelope["subject"], "Тестовое письмо")
        self.assertFalse(envelope["unread"])
        self.assertIn("\\Seen", envelope["flags"])
        self.assertIsInstance(envelope["size_bytes"], int)

    def test_oversized_headers_and_flags_are_bounded(self):
        oversized = "x" * (_MAILBOX.MAX_DECODED_HEADER_CHARS + 200)
        raw_message = _plain_message(subject=oversized)
        long_flag = "f" * (_MAILBOX.MAX_FLAG_CHARS + 50)
        flags = " ".join(
            [long_flag]
            + [f"keyword-{index}" for index in range(_MAILBOX.MAX_FLAG_COUNT + 5)]
            + ["\\Seen"]
        )
        fake = FakeIMAP(search_uids=b"9")
        fake.add_message("9", raw_message, flags=flags)
        mailbox, _factory = _client(fake)

        result = mailbox.list_inbox()

        envelope = result["messages"][0]
        for field in ("from", "to", "subject", "date", "message_id"):
            self.assertLessEqual(
                len(envelope[field]),
                _MAILBOX.MAX_DECODED_HEADER_CHARS,
            )
        self.assertEqual(envelope["header_fields_truncated"], ["subject"])
        self.assertLessEqual(len(envelope["flags"]), _MAILBOX.MAX_FLAG_COUNT)
        self.assertTrue(
            all(len(flag) <= _MAILBOX.MAX_FLAG_CHARS for flag in envelope["flags"])
        )
        self.assertTrue(envelope["flags_truncated"])
        self.assertFalse(envelope["unread"])

    def test_raw_header_fetch_and_parser_are_bounded_before_decode(self):
        oversized_header = (
            b"From: sender@example.com\r\nSubject: "
            + b"x" * (_MAILBOX.MAX_HEADER_BYTES * 2)
            + b"\r\n\r\n"
        )
        fake = FakeIMAP(search_uids=b"11")
        fake.headers["11"] = (oversized_header, len(oversized_header), "")
        mailbox, _factory = _client(fake)
        parsed_lengths = []
        real_parser = _MAILBOX.BytesParser

        class RecordingParser:
            def __init__(self, *args, **kwargs):
                self._delegate = real_parser(*args, **kwargs)

            def parsebytes(self, data, *args, **kwargs):
                parsed_lengths.append(len(data))
                return self._delegate.parsebytes(data, *args, **kwargs)

        with patch.object(_MAILBOX, "BytesParser", RecordingParser):
            result = mailbox.list_inbox()

        self.assertTrue(result["success"])
        self.assertEqual(parsed_lengths, [_MAILBOX.MAX_HEADER_BYTES])
        self.assertTrue(result["messages"][0]["header_bytes_truncated"])
        header_fetches = [
            call[-1]
            for call in fake.calls
            if call[:2] == ("UID", "FETCH") and "HEADER.FIELDS" in call[-1]
        ]
        self.assertEqual(len(header_fetches), 1)
        self.assertIn(f"<0.{_MAILBOX.MAX_HEADER_BYTES}>", header_fetches[0])

    def test_attachment_count_and_filename_lengths_are_bounded(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "reader@yandex.ru"
        message["Subject"] = "Many attachments"
        message.set_content("Visible body")
        oversized_filename = "f" * (
            _MAILBOX.MAX_ATTACHMENT_FILENAME_CHARS + 100
        )
        for index in range(_MAILBOX.MAX_ATTACHMENT_COUNT + 5):
            message.add_attachment(
                b"x",
                maintype="application",
                subtype="octet-stream",
                filename=f"{index}-{oversized_filename}.bin",
            )
        raw_message = message.as_bytes(policy=policy.SMTP)
        fake = FakeIMAP(search_uids=b"19")
        fake.add_message("19", raw_message)
        mailbox, _factory = _client(fake)

        result = mailbox.read_message("19", expected_uidvalidity="777")

        self.assertTrue(result["success"])
        self.assertEqual(
            len(result["attachments"]),
            _MAILBOX.MAX_ATTACHMENT_COUNT,
        )
        self.assertTrue(result["attachments_truncated"])
        self.assertTrue(
            all(
                len(attachment["filename"])
                <= _MAILBOX.MAX_ATTACHMENT_FILENAME_CHARS
                for attachment in result["attachments"]
            )
        )
        self.assertTrue(
            all(
                attachment["filename_truncated"]
                for attachment in result["attachments"]
            )
        )

    def test_read_plain_body_and_only_attachment_metadata(self):
        attachment_secret = b"ATTACHMENT-CONTENT-MUST-NOT-LEAK"
        message = EmailMessage()
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "reader@yandex.ru"
        message["Subject"] = "Multipart"
        message["Message-ID"] = "<multipart@example.com>"
        message.set_content("Visible body text")
        message.add_alternative("<p>HTML alternative</p>", subtype="html")
        message.add_attachment(
            attachment_secret,
            maintype="application",
            subtype="octet-stream",
            filename="секрет.bin",
        )
        raw_message = message.as_bytes(policy=policy.SMTP)
        fake = FakeIMAP(search_uids=b"11")
        fake.add_message("11", raw_message)
        mailbox, _factory = _client(fake)

        result = mailbox.read_message("11", expected_uidvalidity="777")

        self.assertTrue(result["success"])
        self.assertEqual(result["body"], "Visible body text")
        self.assertEqual(result["body_content_type"], "text/plain")
        self.assertEqual(result["attachments"][0]["filename"], "секрет.bin")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(attachment_secret.decode("ascii"), rendered)
        self.assertNotIn("QVRUQUNITUVOVC1DT05URU5ULU1VU1QtTk9ULUxFQUs", rendered)

    def test_attached_email_body_is_never_used_as_message_body(self):
        attached_secret = "ATTACHED-EMAIL-SECRET-MUST-NOT-LEAK"
        attached = EmailMessage()
        attached["From"] = "nested@example.com"
        attached["To"] = "reader@yandex.ru"
        attached["Subject"] = "Attached message"
        attached.set_content(attached_secret)

        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "reader@yandex.ru"
        message["Subject"] = "Outer message"
        message.set_content("<p>Visible outer HTML</p>", subtype="html")
        message.add_attachment(attached, filename="attached.eml")

        raw_message = message.as_bytes(policy=policy.SMTP)
        fake = FakeIMAP(search_uids=b"18")
        fake.add_message("18", raw_message)
        mailbox, _factory = _client(fake)

        result = mailbox.read_message("18", expected_uidvalidity="777")

        self.assertTrue(result["success"])
        self.assertEqual(result["body"], "Visible outer HTML")
        self.assertNotIn(attached_secret, json.dumps(result))
        self.assertEqual(result["attachments"][0]["filename"], "attached.eml")

    def test_html_fallback_ignores_script_and_truncates_body(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "reader@yandex.ru"
        message["Subject"] = "HTML"
        message.set_content(
            "<html><body><p>Hello world</p><script>bad-command</script>"
            "<p>Second paragraph</p></body></html>",
            subtype="html",
        )
        raw_message = message.as_bytes(policy=policy.SMTP)
        fake = FakeIMAP(search_uids=b"12")
        fake.add_message("12", raw_message)
        mailbox, _factory = _client(fake, body_char_limit=8)

        result = mailbox.read_message("12", expected_uidvalidity="777")

        self.assertTrue(result["success"])
        self.assertEqual(result["body_content_type"], "text/html")
        self.assertNotIn("bad-command", result["body"])
        self.assertEqual(len(result["body"]), 8)
        self.assertTrue(result["body_truncated"])
        self.assertGreater(result["body_char_count"], result["body_returned_chars"])

    def test_uidvalidity_mismatch_refuses_fetch(self):
        fake = FakeIMAP(search_uids=b"13", uidvalidity=b"900")
        fake.add_message("13", _plain_message())
        mailbox, _factory = _client(fake)

        result = mailbox.read_message("13", expected_uidvalidity="899")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "STALE_UID")
        fetch_calls = [call for call in fake.calls if call[:2] == ("UID", "FETCH")]
        self.assertEqual(fetch_calls, [])

    def test_uidvalidity_missing_fails_closed_when_expected(self):
        fake = FakeIMAP(search_uids=b"14", uidvalidity=None)
        fake.add_message("14", _plain_message())
        mailbox, _factory = _client(fake)

        result = mailbox.read_message("14", expected_uidvalidity="777")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "UIDVALIDITY_UNAVAILABLE")

    def test_reported_oversize_message_is_not_downloaded(self):
        fake = FakeIMAP(search_uids=b"15")
        fake.add_message("15", _plain_message(), size=5_000)
        mailbox, _factory = _client(fake, max_message_bytes=500)

        result = mailbox.read_message("15", expected_uidvalidity="777")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "MESSAGE_TOO_LARGE")
        fetch_calls = [call for call in fake.calls if call[:2] == ("UID", "FETCH")]
        self.assertEqual(len(fetch_calls), 1)
        self.assertIn("HEADER.FIELDS", fetch_calls[0][-1])

    def test_actual_payload_over_cap_is_rejected_if_server_size_lies(self):
        raw_message = _plain_message(body="x" * 2_000)
        fake = FakeIMAP(search_uids=b"16")
        fake.add_message("16", raw_message, size=100)
        mailbox, _factory = _client(fake, max_message_bytes=500)

        result = mailbox.read_message("16", expected_uidvalidity="777")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "MESSAGE_TOO_LARGE")
        self.assertNotIn("x" * 100, json.dumps(result))

    def test_unknown_message_size_fails_closed(self):
        fake = FakeIMAP(search_uids=b"17")
        fake.add_message("17", _plain_message())
        header, _size, flags = fake.headers["17"]
        fake.headers["17"] = (header, None, flags)
        mailbox, _factory = _client(fake)

        result = mailbox.read_message("17", expected_uidvalidity="777")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "MESSAGE_SIZE_UNKNOWN")

    def test_invalid_arguments_never_open_a_connection(self):
        fake = FakeIMAP()
        mailbox, factory = _client(fake)

        bad_uid = mailbox.read_message("1 STORE +FLAGS \\Deleted")
        missing_uidvalidity = mailbox.read_message("1")
        bad_limit = mailbox.list_inbox(limit=51)
        bad_filter = mailbox.list_inbox(unread_only="yes")
        bad_cursor = mailbox.list_inbox(before_uid="1 OR ALL")
        bad_body_limit = mailbox.read_message(
            "1",
            expected_uidvalidity="777",
            body_char_limit=50_001,
        )

        for label, result in (
            ("uid", bad_uid),
            ("missing_uidvalidity", missing_uidvalidity),
            ("limit", bad_limit),
            ("filter", bad_filter),
            ("cursor", bad_cursor),
            ("body_limit", bad_body_limit),
        ):
            with self.subTest(label=label):
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(factory.calls, [])

    def test_provider_and_imap_errors_never_expose_tokens(self):
        secret = "oauth-super-secret-value"
        fake_provider = FakeIMAP()

        def failing_provider():
            raise RuntimeError(f"failed with {secret}")

        mailbox, _factory = _client(fake_provider, token_provider=failing_provider)
        provider_result = mailbox.list_inbox()

        fake_auth = FakeIMAP(
            auth_error=imaplib.IMAP4.error(f"AUTHENTICATIONFAILED {secret}")
        )
        mailbox, _factory = _client(
            fake_auth,
            token_provider=lambda: ("reader@yandex.ru", secret),
        )
        auth_result = mailbox.list_inbox()

        for result in (provider_result, auth_result):
            rendered = json.dumps(result)
            self.assertFalse(result["success"])
            self.assertNotIn(secret, rendered)
        self.assertEqual(provider_result["error"]["code"], "TOKEN_UNAVAILABLE")
        self.assertEqual(auth_result["error"]["code"], "IMAP_REJECTED")
        self.assertTrue(fake_auth.logout_called)

    def test_missing_message_and_search_failure_are_structured(self):
        missing = FakeIMAP(search_uids=b"99")
        mailbox, _factory = _client(missing)
        missing_result = mailbox.read_message("99", expected_uidvalidity="777")

        failed_search = FakeIMAP(search_status="NO")
        mailbox, _factory = _client(failed_search)
        search_result = mailbox.list_inbox()

        self.assertEqual(missing_result["error"]["code"], "MESSAGE_NOT_FOUND")
        self.assertEqual(search_result["error"]["code"], "SEARCH_FAILED")
        self.assertTrue(missing.logout_called)
        self.assertTrue(failed_search.logout_called)

    def test_every_result_carries_untrusted_email_notice(self):
        fake = FakeIMAP(search_uids=b"")
        mailbox, _factory = _client(fake)

        success = mailbox.list_inbox()
        failure = mailbox.read_message("not-a-uid")

        self.assertEqual(success["security_notice"], SECURITY_NOTICE)
        self.assertEqual(failure["security_notice"], SECURITY_NOTICE)
        self.assertIn("untrusted", SECURITY_NOTICE.lower())
        self.assertIn("not instructions", SECURITY_NOTICE.lower())


if __name__ == "__main__":
    unittest.main()
