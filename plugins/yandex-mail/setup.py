"""Command-line setup for the read-only Yandex Mail OAuth integration."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Optional, Sequence, TextIO

import auth as yandex_auth


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--credentials",
        default=str(yandex_auth.DEFAULT_CREDENTIALS_PATH),
        help="OAuth client JSON path (default: %(default)s)",
    )
    parser.add_argument(
        "--tokens",
        default=str(yandex_auth.DEFAULT_TOKEN_PATH),
        help="OAuth token JSON path (default: %(default)s)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure read-only Yandex Mail OAuth access."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_url = subparsers.add_parser(
        "auth-url",
        help="print the Yandex authorization URL",
    )
    _add_paths(auth_url)

    auth_code = subparsers.add_parser(
        "auth-code",
        help="securely prompt for and exchange a code or complete callback URL",
    )
    _add_paths(auth_code)

    status = subparsers.add_parser(
        "status",
        help="show local OAuth status without exposing tokens",
    )
    _add_paths(status)

    check_live = subparsers.add_parser(
        "check-live",
        help="refresh if needed and verify read-only IMAP access",
    )
    _add_paths(check_live)
    check_live.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="IMAP connection timeout in seconds (default: %(default)s)",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    args = _parser().parse_args(argv)

    try:
        if args.command == "auth-url":
            print(yandex_auth.build_authorization_url(args.credentials), file=output)
            return 0

        if args.command == "auth-code":
            code_or_callback_url = getpass.getpass(
                "Yandex authorization code: ",
                stream=errors,
            )
            yandex_auth.exchange_authorization_code(
                code_or_callback_url,
                args.credentials,
                args.tokens,
            )
            print("Yandex Mail authorization saved.", file=output)
            return 0

        if args.command == "status":
            status = yandex_auth.get_status(args.credentials, args.tokens)
            print(
                f"authorized: {'yes' if status['authorized'] else 'no'}",
                file=output,
            )
            if status["authorized"]:
                print(f"scope: {status['scope']}", file=output)
                print(
                    f"access token valid: {'yes' if status['valid'] else 'no'}",
                    file=output,
                )
                print(
                    "refresh token available: "
                    f"{'yes' if status['refresh_available'] else 'no'}",
                    file=output,
                )
                print(
                    f"expires in seconds: {status['expires_in_seconds']}",
                    file=output,
                )
            return 0

        if args.command == "check-live":
            yandex_auth.check_live(
                args.credentials,
                args.tokens,
                timeout=args.timeout,
            )
            print("Yandex Mail read-only IMAP check: ok", file=output)
            return 0

        raise AssertionError("unreachable command")
    except yandex_auth.YandexMailAuthError as exc:
        print(f"error: {exc}", file=errors)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
