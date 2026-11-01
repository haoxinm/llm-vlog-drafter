"""Minimal HTTP transport (stdlib only) behind a tiny injectable interface, so tests never touch the network."""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from trip_vlog.config import is_loopback_host

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]   # lower-case names
    body: bytes = b""


class TransportError(Exception):
    """Base for transport failures that produced no HTTP response."""


class TransportTimeout(TransportError): ...
class TransportConnectionError(TransportError): ...


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse:
        """Return the response for ANY status code (4xx/5xx included); raise TransportError subclasses otherwise."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: a redirect could carry the API key to another host
        return None


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)
        # loopback servers are always contacted directly, never through an environment-configured proxy
        self._direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def send(self, request: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers), method=request.method)
        try:
            host = urlsplit(request.url).hostname or ""
            opener = self._direct if is_loopback_host(host) else self._opener
            with opener.open(req, timeout=request.timeout) as r:
                return HttpResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as e:  # non-2xx (and unfollowed redirects)
            try:
                body = e.read(MAX_RESPONSE_BYTES)
            except (OSError, TimeoutError):
                body = b""
            return HttpResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeout("request timed out") from None
            raise TransportConnectionError(f"{type(e.reason).__name__}: {e.reason}") from None
        except (socket.timeout, TimeoutError):
            raise TransportTimeout("request timed out") from None
        except (OSError, ValueError, http.client.HTTPException) as e:  # connection reset, malformed status line
            raise TransportConnectionError(f"{type(e).__name__}: {e}") from None
