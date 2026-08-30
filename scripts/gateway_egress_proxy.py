#!/usr/bin/env python3
"""Loopback-only, fail-closed egress proxy for the Telegram browser.

The browser is an untrusted web client.  URL pre-checks alone cannot stop a
redirect or DNS-rebinding target from changing between validation and connect.
This proxy owns both operations: it resolves each destination once, validates
*all* returned socket addresses, and connects using only a validated numeric
sockaddr.  It intentionally supports plain HTTP on port 80 and CONNECT on port
443; every other destination or proxy protocol feature is denied.

The module has no Hermes dependency so its policy can be unit-tested with the
standard library.  It never logs request URLs, headers, coordinates, or secrets.
"""

from __future__ import annotations

import ipaddress
import queue
import re
import selectors
import socket
import threading
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Callable, Iterable, Optional, Sequence, Type
from urllib.parse import urlsplit


_MAX_HEADER_BYTES = 16 * 1024
_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
_MAX_TUNNEL_BUFFER = 256 * 1024
_HEADER_TIMEOUT_SECONDS = 5.0
_DNS_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_SECONDS = 8.0
_IO_IDLE_TIMEOUT_SECONDS = 90.0
_CONNECTION_LIFETIME_SECONDS = 300.0
_MAX_CONNECTIONS = 32
_RESOLVER_WORKERS = 4
_RESOLVER_QUEUE_SIZE = 16

_TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class _ProxyError(Exception):
    def __init__(self, status: int, reason: str, body: bytes) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.body = body


class _BadRequest(_ProxyError):
    def __init__(self, body: bytes = b"Malformed proxy request.\n") -> None:
        super().__init__(400, "Bad Request", body)


class _PolicyDenied(_ProxyError):
    def __init__(self, body: bytes = b"Destination is not allowed.\n") -> None:
        super().__init__(403, "Forbidden", body)


class _UpstreamFailure(_ProxyError):
    def __init__(self, body: bytes = b"Upstream connection failed.\n") -> None:
        super().__init__(502, "Bad Gateway", body)


@dataclass(frozen=True)
class _Authority:
    host: str
    port: int


@dataclass(frozen=True)
class _ParsedRequest:
    method: str
    version: str
    authority: _Authority
    origin_target: Optional[str]
    headers: tuple[tuple[str, str], ...]
    content_length: int
    initial_body: bytes


Resolver = Callable[[str, int, int, int, int], Sequence[tuple]]
Connector = Callable[[tuple, float], socket.socket]


def _contains_forbidden_text_character(value: str, *, allow_tab: bool) -> bool:
    for character in value:
        codepoint = ord(character)
        if codepoint == 0x7F or codepoint < 0x20:
            if allow_tab and character == "\t":
                continue
            return True
    return False


def _canonical_host(host: str) -> str:
    if not host or "%" in host or "\\" in host:
        raise _BadRequest()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _BadRequest() from exc
    ascii_host = ascii_host.lower().rstrip(".")
    if not ascii_host:
        raise _BadRequest()
    try:
        parsed_ip = ipaddress.ip_address(ascii_host)
    except ValueError:
        if not _HOST_RE.fullmatch(ascii_host):
            raise _BadRequest()
        return ascii_host
    if isinstance(parsed_ip, ipaddress.IPv6Address) and parsed_ip.ipv4_mapped:
        return str(parsed_ip.ipv4_mapped)
    return parsed_ip.compressed


def _parse_authority(
    raw_authority: str,
    *,
    default_port: int,
    require_explicit_port: bool,
) -> _Authority:
    if (
        not raw_authority
        or "@" in raw_authority
        or "/" in raw_authority
        or "?" in raw_authority
        or "#" in raw_authority
        or _contains_forbidden_text_character(raw_authority, allow_tab=False)
    ):
        raise _BadRequest()

    explicit_port: Optional[str]
    if raw_authority.startswith("["):
        closing = raw_authority.find("]")
        if closing <= 1:
            raise _BadRequest()
        host_text = raw_authority[1:closing]
        remainder = raw_authority[closing + 1 :]
        if not remainder:
            explicit_port = None
        elif remainder.startswith(":") and remainder.count(":") == 1:
            explicit_port = remainder[1:]
        else:
            raise _BadRequest()
    else:
        if raw_authority.count(":") > 1:
            raise _BadRequest()
        if ":" in raw_authority:
            host_text, explicit_port = raw_authority.rsplit(":", 1)
        else:
            host_text, explicit_port = raw_authority, None

    if require_explicit_port and explicit_port is None:
        raise _BadRequest()
    if explicit_port is None:
        port = default_port
    else:
        if not explicit_port or not explicit_port.isascii() or not explicit_port.isdecimal():
            raise _BadRequest()
        port = int(explicit_port)
        if not 1 <= port <= 65535:
            raise _BadRequest()
    return _Authority(_canonical_host(host_text), port)


def _read_header_block(client: socket.socket) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + _HEADER_TIMEOUT_SECONDS
    data = bytearray()
    while b"\r\n\r\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        client.settimeout(remaining)
        if len(data) >= _MAX_HEADER_BYTES:
            raise _ProxyError(431, "Request Header Fields Too Large", b"Headers are too large.\n")
        chunk = client.recv(min(4096, _MAX_HEADER_BYTES + 4 - len(data)))
        if not chunk:
            raise _BadRequest()
        data.extend(chunk)
    header, remainder = bytes(data).split(b"\r\n\r\n", 1)
    if len(header) + 4 > _MAX_HEADER_BYTES:
        raise _ProxyError(431, "Request Header Fields Too Large", b"Headers are too large.\n")
    return header, remainder


def _parse_headers(lines: Sequence[bytes]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for raw_line in lines:
        if not raw_line or raw_line[:1] in {b" ", b"\t"} or b":" not in raw_line:
            raise _BadRequest()
        raw_name, raw_value = raw_line.split(b":", 1)
        if not _TOKEN_RE.fullmatch(raw_name):
            raise _BadRequest()
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("latin-1").strip(" \t")
        except UnicodeError as exc:
            raise _BadRequest() from exc
        if _contains_forbidden_text_character(value, allow_tab=True):
            raise _BadRequest()
        parsed.append((name, value))
    return parsed


def _single_header(headers: Iterable[tuple[str, str]], name: str) -> Optional[str]:
    values = [value for header_name, value in headers if header_name == name]
    if len(values) > 1:
        raise _BadRequest()
    return values[0] if values else None


def _parse_content_length(headers: Sequence[tuple[str, str]]) -> int:
    values = [value for name, value in headers if name == "content-length"]
    if any(name == "transfer-encoding" for name, _ in headers):
        # Chunked request bodies are deliberately unsupported.  This also
        # rejects CL+TE request-smuggling ambiguity.
        raise _BadRequest(b"Transfer-encoded requests are not supported.\n")
    if not values:
        return 0
    if len(set(values)) != 1:
        raise _BadRequest()
    value = values[0]
    if not value.isascii() or not value.isdecimal():
        raise _BadRequest()
    length = int(value)
    if length > _MAX_REQUEST_BODY_BYTES:
        raise _ProxyError(413, "Content Too Large", b"Request body is too large.\n")
    return length


def _connection_tokens(headers: Sequence[tuple[str, str]]) -> set[str]:
    result: set[str] = set()
    for name, value in headers:
        if name != "connection":
            continue
        for token in value.split(","):
            normalized = token.strip().lower()
            if (
                not normalized
                or not normalized.isascii()
                or not _TOKEN_RE.fullmatch(normalized.encode("ascii"))
            ):
                raise _BadRequest()
            result.add(normalized)
    if result & {"host", "content-length"}:
        raise _BadRequest(b"Connection header names a framing field.\n")
    return result


def _parse_request(header: bytes, initial_body: bytes) -> _ParsedRequest:
    lines = header.split(b"\r\n")
    if not lines:
        raise _BadRequest()
    parts = lines[0].split(b" ")
    if len(parts) != 3 or not _TOKEN_RE.fullmatch(parts[0]):
        raise _BadRequest()
    try:
        method = parts[0].decode("ascii").upper()
        target = parts[1].decode("ascii")
        version = parts[2].decode("ascii")
    except UnicodeError as exc:
        raise _BadRequest() from exc
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise _ProxyError(505, "HTTP Version Not Supported", b"Unsupported HTTP version.\n")
    if _contains_forbidden_text_character(target, allow_tab=False):
        raise _BadRequest()

    headers = _parse_headers(lines[1:])
    _connection_tokens(headers)
    host_header = _single_header(headers, "host")
    if host_header is None:
        raise _BadRequest()
    if _single_header(headers, "expect") is not None:
        raise _ProxyError(417, "Expectation Failed", b"Expect is not supported.\n")

    if method == "CONNECT":
        authority = _parse_authority(target, default_port=443, require_explicit_port=True)
        if authority.port != 443:
            raise _PolicyDenied()
        header_authority = _parse_authority(
            host_header,
            default_port=443,
            require_explicit_port=False,
        )
        if header_authority != authority or initial_body:
            raise _BadRequest()
        return _ParsedRequest(method, version, authority, None, tuple(headers), 0, b"")

    if "\\" in target or "#" in target:
        raise _BadRequest()
    try:
        split = urlsplit(target)
        parsed_port = split.port
    except ValueError as exc:
        raise _BadRequest() from exc
    if split.scheme.lower() != "http" or not split.netloc or split.username is not None or split.password is not None:
        raise _BadRequest(b"Only absolute-form HTTP requests are supported.\n")
    if split.fragment:
        raise _BadRequest()
    authority = _Authority(_canonical_host(split.hostname or ""), parsed_port or 80)
    if authority.port != 80:
        raise _PolicyDenied()
    header_authority = _parse_authority(
        host_header,
        default_port=80,
        require_explicit_port=False,
    )
    if header_authority != authority:
        raise _BadRequest(b"Host does not match the request target.\n")

    content_length = _parse_content_length(headers)
    if len(initial_body) > content_length:
        raise _BadRequest(b"Unexpected bytes after request body.\n")
    origin_target = split.path or "/"
    if split.query:
        origin_target += "?" + split.query
    return _ParsedRequest(
        method,
        version,
        authority,
        origin_target,
        tuple(headers),
        content_length,
        initial_body,
    )


def _is_public_address(address: str) -> bool:
    if "%" in address:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global


def _validate_address_infos(address_infos: Sequence[tuple]) -> tuple[tuple, ...]:
    if not address_infos:
        raise _UpstreamFailure()
    validated: list[tuple] = []
    seen: set[tuple] = set()
    for item in address_infos:
        if not isinstance(item, tuple) or len(item) != 5:
            raise _UpstreamFailure()
        family, socktype, proto, canonname, sockaddr = item
        if family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM:
            raise _PolicyDenied()
        if not isinstance(sockaddr, tuple) or not sockaddr:
            raise _UpstreamFailure()
        address = sockaddr[0]
        if not isinstance(address, str) or not _is_public_address(address):
            # Validate the complete DNS answer before attempting any connect.
            # A mixed public/private answer is denied as a whole.
            raise _PolicyDenied()
        if family == socket.AF_INET6 and len(sockaddr) >= 4 and sockaddr[3] != 0:
            raise _PolicyDenied()
        key = (family, socktype, proto, sockaddr)
        if key not in seen:
            seen.add(key)
            validated.append((family, socktype, proto, canonname, sockaddr))
    if not validated:
        raise _UpstreamFailure()
    return tuple(validated)


class _ResolverPool:
    """Small daemon resolver pool; stalled libc lookups cannot grow threads."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._queue: queue.Queue[object] = queue.Queue(_RESOLVER_QUEUE_SIZE)
        self._closed = threading.Event()
        self._workers: list[threading.Thread] = []
        for index in range(_RESOLVER_WORKERS):
            worker = threading.Thread(
                target=self._run,
                name=f"gateway-proxy-dns-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                return
            host, port, reply = item  # type: ignore[misc]
            try:
                value = self._resolver(
                    host,
                    port,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                )
                reply.put((True, value))
            except BaseException as exc:  # resolver failure is data, not a worker crash
                reply.put((False, exc))

    def resolve(self, host: str, port: int) -> tuple[tuple, ...]:
        if self._closed.is_set():
            raise _UpstreamFailure()
        reply: queue.Queue[tuple[bool, object]] = queue.Queue(1)
        try:
            self._queue.put_nowait((host, port, reply))
        except queue.Full as exc:
            raise _UpstreamFailure() from exc
        try:
            success, value = reply.get(timeout=_DNS_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise _UpstreamFailure() from exc
        if not success:
            raise _UpstreamFailure() from value if isinstance(value, BaseException) else None
        return _validate_address_infos(value)  # type: ignore[arg-type]

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break


def _default_resolver(host: str, port: int, family: int, socktype: int, proto: int) -> Sequence[tuple]:
    return socket.getaddrinfo(host, port, family, socktype, proto)


def _default_connector(address_info: tuple, timeout: float) -> socket.socket:
    family, socktype, proto, _canonname, sockaddr = address_info
    upstream = socket.socket(family, socktype, proto)
    upstream.settimeout(timeout)
    try:
        # sockaddr came directly from the single, fully validated getaddrinfo
        # result.  Passing it to connect performs no second DNS lookup.
        upstream.connect(sockaddr)
    except BaseException:
        upstream.close()
        raise
    return upstream


def _send_error(client: socket.socket, error: _ProxyError) -> None:
    response = (
        f"HTTP/1.1 {error.status} {error.reason}\r\n"
        f"Content-Length: {len(error.body)}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + error.body
    try:
        client.settimeout(1.0)
        client.sendall(response)
    except OSError:
        pass


def _upstream_headers(request: _ParsedRequest) -> bytes:
    connection_names = _connection_tokens(request.headers)
    excluded = _HOP_BY_HOP | connection_names | {"host", "content-length"}
    lines = [
        f"{request.method} {request.origin_target} {request.version}",
        "Host: " + next(value for name, value in request.headers if name == "host"),
    ]
    for name, value in request.headers:
        if name in excluded or name.startswith("proxy-"):
            continue
        lines.append(f"{name}: {value}")
    if any(name == "content-length" for name, _ in request.headers):
        lines.append(f"Content-Length: {request.content_length}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class GatewayEgressProxy:
    """Bounded loopback proxy with a process-lifetime public-IP policy."""

    def __init__(
        self,
        *,
        resolver: Resolver = _default_resolver,
        connector: Connector = _default_connector,
        max_connections: int = _MAX_CONNECTIONS,
    ) -> None:
        if max_connections < 1 or max_connections > 256:
            raise ValueError("invalid proxy connection limit")
        self._resolver_pool = _ResolverPool(resolver)
        self._connector = connector
        self._capacity = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._active_lock = threading.Lock()
        self._active: set[socket.socket] = set()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(min(max_connections * 2, 128))
        self._listener.settimeout(0.5)
        self._port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="gateway-egress-proxy",
            daemon=True,
        )
        self._thread.start()
        if not self._thread.is_alive():
            self.close()
            raise RuntimeError("gateway egress proxy failed to start")

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def is_healthy(self) -> bool:
        return not self._stop.is_set() and self._thread.is_alive() and self._listener.fileno() >= 0

    def _track(self, connection: socket.socket) -> None:
        with self._active_lock:
            self._active.add(connection)

    def _untrack(self, connection: socket.socket) -> None:
        with self._active_lock:
            self._active.discard(connection)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if address[0] != "127.0.0.1":
                client.close()
                continue
            if not self._capacity.acquire(blocking=False):
                _send_error(client, _ProxyError(503, "Service Unavailable", b"Proxy is busy.\n"))
                client.close()
                continue
            self._track(client)
            threading.Thread(
                target=self._serve_and_release,
                args=(client,),
                name="gateway-proxy-client",
                daemon=True,
            ).start()

    def _serve_and_release(self, client: socket.socket) -> None:
        try:
            self._serve(client)
        finally:
            self._untrack(client)
            try:
                client.close()
            finally:
                self._capacity.release()

    def _connect(self, authority: _Authority, deadline: float) -> socket.socket:
        address_infos = self._resolver_pool.resolve(authority.host, authority.port)
        last_error: Optional[BaseException] = None
        for address_info in address_infos:
            remaining = min(_CONNECT_TIMEOUT_SECONDS, deadline - time.monotonic())
            if remaining <= 0:
                break
            try:
                upstream = self._connector(address_info, remaining)
                self._track(upstream)
                return upstream
            except (OSError, TimeoutError) as exc:
                last_error = exc
        raise _UpstreamFailure() from last_error

    def _serve(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        committed = False

        def mark_committed() -> None:
            nonlocal committed
            committed = True

        try:
            deadline = time.monotonic() + _CONNECTION_LIFETIME_SECONDS
            header, initial_body = _read_header_block(client)
            request = _parse_request(header, initial_body)
            upstream = self._connect(request.authority, deadline)
            if request.method == "CONNECT":
                mark_committed()
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay_tunnel(client, upstream, deadline)
            else:
                self._forward_http(
                    client,
                    upstream,
                    request,
                    deadline,
                    mark_committed,
                )
        except _ProxyError as error:
            if not committed:
                _send_error(client, error)
        except (OSError, TimeoutError):
            if not committed:
                _send_error(client, _UpstreamFailure())
        finally:
            if upstream is not None:
                self._untrack(upstream)
                upstream.close()

    def _forward_http(
        self,
        client: socket.socket,
        upstream: socket.socket,
        request: _ParsedRequest,
        deadline: float,
        mark_committed: Callable[[], None],
    ) -> None:
        upstream.settimeout(_IO_IDLE_TIMEOUT_SECONDS)
        upstream.sendall(_upstream_headers(request))
        sent = len(request.initial_body)
        if request.initial_body:
            upstream.sendall(request.initial_body)
        client.settimeout(_IO_IDLE_TIMEOUT_SECONDS)
        while sent < request.content_length:
            remaining_lifetime = deadline - time.monotonic()
            if remaining_lifetime <= 0:
                raise TimeoutError()
            client.settimeout(min(_IO_IDLE_TIMEOUT_SECONDS, remaining_lifetime))
            chunk = client.recv(min(64 * 1024, request.content_length - sent))
            if not chunk:
                raise _BadRequest(b"Incomplete request body.\n")
            upstream.sendall(chunk)
            sent += len(chunk)
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass

        committed_response = False
        while True:
            remaining_lifetime = deadline - time.monotonic()
            if remaining_lifetime <= 0:
                raise TimeoutError()
            upstream.settimeout(min(_IO_IDLE_TIMEOUT_SECONDS, remaining_lifetime))
            chunk = upstream.recv(64 * 1024)
            if not chunk:
                if not committed_response:
                    raise _UpstreamFailure()
                return
            if not committed_response:
                mark_committed()
                committed_response = True
            client.sendall(chunk)

    def _relay_tunnel(self, client: socket.socket, upstream: socket.socket, deadline: float) -> None:
        sockets = (client, upstream)
        peer = {client: upstream, upstream: client}
        pending = {client: bytearray(), upstream: bytearray()}
        read_open = {client: True, upstream: True}
        shutdown_pending = {client: False, upstream: False}
        last_activity = time.monotonic()
        for connection in sockets:
            connection.setblocking(False)

        with selectors.DefaultSelector() as selector:
            while True:
                now = time.monotonic()
                if now >= deadline or now - last_activity >= _IO_IDLE_TIMEOUT_SECONDS:
                    raise TimeoutError()
                if not any(read_open.values()) and not any(pending.values()):
                    return

                for connection in sockets:
                    if self._stop.is_set() or connection.fileno() < 0:
                        return
                    events = 0
                    if read_open[connection] and len(pending[peer[connection]]) < _MAX_TUNNEL_BUFFER:
                        events |= selectors.EVENT_READ
                    if pending[connection]:
                        events |= selectors.EVENT_WRITE
                    try:
                        selector.unregister(connection)
                    except (KeyError, ValueError, OSError):
                        pass
                    if events:
                        try:
                            selector.register(connection, events)
                        except (ValueError, OSError):
                            # Proxy shutdown closes active descriptors from the
                            # owner thread.  The fixed child proxy URL then
                            # fails closed; the relay exits without a traceback.
                            return

                timeout = min(1.0, deadline - now, _IO_IDLE_TIMEOUT_SECONDS - (now - last_activity))
                for key, events in selector.select(max(0.01, timeout)):
                    connection = key.fileobj
                    if events & selectors.EVENT_READ:
                        try:
                            data = connection.recv(
                                min(64 * 1024, _MAX_TUNNEL_BUFFER - len(pending[peer[connection]]))
                            )
                        except BlockingIOError:
                            data = None
                        if data:
                            pending[peer[connection]].extend(data)
                            last_activity = time.monotonic()
                        elif data == b"":
                            read_open[connection] = False
                            shutdown_pending[peer[connection]] = True
                    if events & selectors.EVENT_WRITE and pending[connection]:
                        try:
                            count = connection.send(pending[connection])
                        except BlockingIOError:
                            count = 0
                        if count:
                            del pending[connection][:count]
                            last_activity = time.monotonic()

                for connection in sockets:
                    if shutdown_pending[connection] and not pending[connection]:
                        try:
                            connection.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        shutdown_pending[connection] = False

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._resolver_pool.close()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._active_lock:
            active = tuple(self._active)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self._thread.join(timeout=1.0)

    def __enter__(self) -> "GatewayEgressProxy":
        return self

    def __exit__(
        self,
        _exc_type: Optional[Type[BaseException]],
        _exc: Optional[BaseException],
        _traceback: Optional[TracebackType],
    ) -> None:
        self.close()
