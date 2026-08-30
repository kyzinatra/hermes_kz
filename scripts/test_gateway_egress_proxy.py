from __future__ import annotations

import socket
import threading
import time
import unittest
from typing import Callable, Sequence

try:
    from scripts.gateway_egress_proxy import GatewayEgressProxy
except ImportError:
    from gateway_egress_proxy import GatewayEgressProxy


def _address_info(address: str, port: int = 80) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def _proxy_port(proxy: GatewayEgressProxy) -> int:
    return int(proxy.proxy_url.rsplit(":", 1)[1])


def _request(proxy: GatewayEgressProxy, payload: bytes) -> bytes:
    client = socket.create_connection(("127.0.0.1", _proxy_port(proxy)), timeout=2)
    try:
        client.settimeout(2)
        client.sendall(payload)
        response = bytearray()
        while True:
            try:
                chunk = client.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            response.extend(chunk)
        return bytes(response)
    finally:
        client.close()


class _OneShotOrigin:
    def __init__(self, response: bytes) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.response = response
        self.request = b""
        self.hit_count = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
        except OSError:
            self._done.set()
            return
        with connection:
            connection.settimeout(2)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            self.request = bytes(data)
            self.hit_count += 1
            connection.sendall(self.response)
        self._done.set()

    def connector(self, _address_info_value: tuple, timeout: float) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)

    def wait(self) -> None:
        self._done.wait(2)

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=1)


class GatewayEgressProxyTests(unittest.TestCase):
    def _proxy(
        self,
        *,
        resolver: Callable[[str, int, int, int, int], Sequence[tuple]],
        connector: Callable[[tuple, float], socket.socket],
    ) -> GatewayEgressProxy:
        proxy = GatewayEgressProxy(resolver=resolver, connector=connector)
        self.addCleanup(proxy.close)
        return proxy

    def test_private_and_mixed_dns_answers_never_reach_connector(self) -> None:
        connector_calls = []

        def resolver(host: str, port: int, *_args: int) -> Sequence[tuple]:
            if host == "private.test":
                return [_address_info("127.0.0.1", port)]
            return [
                _address_info("8.8.8.8", port),
                _address_info("10.0.0.8", port),
            ]

        def connector(address_info_value: tuple, _timeout: float) -> socket.socket:
            connector_calls.append(address_info_value)
            raise AssertionError("blocked destination must never connect")

        proxy = self._proxy(resolver=resolver, connector=connector)
        for host in ("private.test", "mixed.test"):
            response = _request(
                proxy,
                f"GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\n\r\n".encode(),
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 403"), response)
        self.assertEqual(connector_calls, [])

    def test_http_resolves_once_connects_numeric_and_rebuilds_headers(self) -> None:
        origin = _OneShotOrigin(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
        )
        self.addCleanup(origin.close)
        resolver_calls = []
        connector_calls = []

        def resolver(host: str, port: int, *_args: int) -> Sequence[tuple]:
            resolver_calls.append((host, port))
            return [_address_info("8.8.8.8", port)]

        def connector(address_info_value: tuple, timeout: float) -> socket.socket:
            connector_calls.append(address_info_value)
            return origin.connector(address_info_value, timeout)

        proxy = self._proxy(resolver=resolver, connector=connector)
        response = _request(
            proxy,
            b"GET http://public.test/path?q=1 HTTP/1.1\r\n"
            b"Host: public.test\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"Proxy-Secret: remove-me\r\n"
            b"Connection: x-remove\r\n"
            b"X-Remove: secret\r\n"
            b"X-Keep: yes\r\n\r\n",
        )
        origin.wait()

        self.assertIn(b"200 OK", response)
        self.assertEqual(resolver_calls, [("public.test", 80)])
        self.assertEqual(connector_calls[0][4], ("8.8.8.8", 80))
        self.assertTrue(origin.request.startswith(b"GET /path?q=1 HTTP/1.1\r\n"))
        self.assertIn(b"Host: public.test\r\n", origin.request)
        self.assertIn(b"x-keep: yes\r\n", origin.request.lower())
        self.assertIn(b"Connection: close\r\n", origin.request)
        self.assertNotIn(b"proxy-connection", origin.request.lower())
        self.assertNotIn(b"proxy-secret", origin.request.lower())
        self.assertNotIn(b"x-remove", origin.request.lower())

    def test_hypothetical_second_dns_answer_is_never_requested(self) -> None:
        resolver_calls = 0
        origin = _OneShotOrigin(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        self.addCleanup(origin.close)

        def resolver(_host: str, port: int, *_args: int) -> Sequence[tuple]:
            nonlocal resolver_calls
            resolver_calls += 1
            if resolver_calls > 1:
                return [_address_info("127.0.0.1", port)]
            return [_address_info("8.8.8.8", port)]

        proxy = self._proxy(resolver=resolver, connector=origin.connector)
        response = _request(
            proxy,
            b"GET http://rebind.test/ HTTP/1.1\r\nHost: rebind.test\r\n\r\n",
        )
        self.assertIn(b"204 No Content", response)
        self.assertEqual(resolver_calls, 1)

    def test_plain_http_redirect_second_hop_to_private_is_denied_before_connect(self) -> None:
        redirect = _OneShotOrigin(
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        self.addCleanup(redirect.close)
        connector_calls = []

        def resolver(host: str, port: int, *_args: int) -> Sequence[tuple]:
            if host == "public.test":
                return [_address_info("8.8.8.8", port)]
            return [_address_info("127.0.0.1", port)]

        def connector(address_info_value: tuple, timeout: float) -> socket.socket:
            connector_calls.append(address_info_value)
            return redirect.connector(address_info_value, timeout)

        proxy = self._proxy(resolver=resolver, connector=connector)
        first = _request(
            proxy,
            b"GET http://public.test/start HTTP/1.1\r\nHost: public.test\r\n\r\n",
        )
        second = _request(
            proxy,
            b"GET http://127.0.0.1/private HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        )
        self.assertIn(b"302 Found", first)
        self.assertTrue(second.startswith(b"HTTP/1.1 403"), second)
        self.assertEqual(len(connector_calls), 1)

    def test_connect_tunnel_uses_validated_endpoint_and_relays_both_ways(self) -> None:
        peer_holder = []

        def resolver(_host: str, port: int, *_args: int) -> Sequence[tuple]:
            return [_address_info("8.8.8.8", port)]

        def connector(_address_info_value: tuple, _timeout: float) -> socket.socket:
            proxy_side, origin_side = socket.socketpair()
            peer_holder.append(origin_side)

            def echo() -> None:
                with origin_side:
                    data = origin_side.recv(4)
                    origin_side.sendall(data.upper())

            threading.Thread(target=echo, daemon=True).start()
            return proxy_side

        proxy = self._proxy(resolver=resolver, connector=connector)
        client = socket.create_connection(("127.0.0.1", _proxy_port(proxy)), timeout=2)
        self.addCleanup(client.close)
        client.settimeout(2)
        client.sendall(
            b"CONNECT public.test:443 HTTP/1.1\r\nHost: public.test:443\r\n\r\n"
        )
        response = client.recv(4096)
        self.assertIn(b"200 Connection Established", response)
        client.sendall(b"ping")
        self.assertEqual(client.recv(4), b"PING")

    def test_blocked_connect_never_returns_200_or_calls_connector(self) -> None:
        connector_calls = []

        def resolver(_host: str, port: int, *_args: int) -> Sequence[tuple]:
            return [_address_info("169.254.169.254", port)]

        def connector(value: tuple, _timeout: float) -> socket.socket:
            connector_calls.append(value)
            raise AssertionError("must not connect")

        proxy = self._proxy(resolver=resolver, connector=connector)
        response = _request(
            proxy,
            b"CONNECT metadata.test:443 HTTP/1.1\r\nHost: metadata.test:443\r\n\r\n",
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 403"), response)
        self.assertNotIn(b"200 Connection Established", response)
        self.assertEqual(connector_calls, [])

    def test_malformed_targets_ports_and_smuggling_are_rejected(self) -> None:
        connector_calls = []

        def resolver(_host: str, port: int, *_args: int) -> Sequence[tuple]:
            return [_address_info("8.8.8.8", port)]

        def connector(value: tuple, _timeout: float) -> socket.socket:
            connector_calls.append(value)
            raise AssertionError("malformed request must not connect")

        proxy = self._proxy(resolver=resolver, connector=connector)
        requests = (
            b"CONNECT public.test:22 HTTP/1.1\r\nHost: public.test:22\r\n\r\n",
            b"CONNECT http://public.test:443 HTTP/1.1\r\nHost: public.test:443\r\n\r\n",
            b"GET http://user:pass@public.test/ HTTP/1.1\r\nHost: public.test\r\n\r\n",
            b"GET http://public.test:8080/ HTTP/1.1\r\nHost: public.test:8080\r\n\r\n",
            b"GET http://public.test/ HTTP/1.1\r\nHost: other.test\r\n\r\n",
            b"GET http://public.test/ HTTP/1.1\r\nHost: public.test\r\nHost: public.test\r\n\r\n",
            b"POST http://public.test/ HTTP/1.1\r\nHost: public.test\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"POST http://public.test/ HTTP/1.1\r\nHost: public.test\r\nConnection: content-length\r\nContent-Length: 1\r\n\r\nx",
            b"GET http://[fe80::1%25eth0]/ HTTP/1.1\r\nHost: [fe80::1%25eth0]\r\n\r\n",
            b"GET http://public.test\\@127.0.0.1/ HTTP/1.1\r\nHost: public.test\r\n\r\n",
            b"GET http://public.test/a\x00b HTTP/1.1\r\nHost: public.test\r\n\r\n",
        )
        for payload in requests:
            with self.subTest(payload=payload.split(b"\r\n", 1)[0]):
                response = _request(proxy, payload)
                self.assertTrue(
                    response.startswith((b"HTTP/1.1 400", b"HTTP/1.1 403")),
                    response,
                )
        self.assertEqual(connector_calls, [])

    def test_oversized_header_is_bounded_and_proxy_remains_healthy(self) -> None:
        def resolver(_host: str, port: int, *_args: int) -> Sequence[tuple]:
            return [_address_info("8.8.8.8", port)]

        proxy = self._proxy(
            resolver=resolver,
            connector=lambda *_: (_ for _ in ()).throw(AssertionError("must not connect")),
        )
        prefix = b"GET http://public.test/ HTTP/1.1\r\nHost: public.test\r\nX: "
        # Fill the cap exactly without a terminator. There are no unread TCP
        # bytes when the proxy closes, so Windows cannot replace the 431 with
        # a reset caused by discarded client data.
        response = _request(proxy, prefix + b"a" * (16 * 1024 - len(prefix)))
        self.assertTrue(response.startswith(b"HTTP/1.1 431"), response[:100])
        self.assertTrue(proxy.is_healthy)

    def test_close_removes_listener_without_direct_fallback(self) -> None:
        proxy = GatewayEgressProxy(
            resolver=lambda _host, port, *_: [_address_info("8.8.8.8", port)],
            connector=lambda *_: (_ for _ in ()).throw(AssertionError()),
        )
        port = _proxy_port(proxy)
        proxy.close()
        self.assertFalse(proxy.is_healthy)
        deadline = time.monotonic() + 1
        while True:
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.1)
            if time.monotonic() >= deadline:
                break
            # A single successful refusal is enough; the loop only avoids a
            # platform-specific close race without sleeping for long.
            break


if __name__ == "__main__":
    unittest.main()
