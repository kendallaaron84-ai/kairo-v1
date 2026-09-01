from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class FeedHttpError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""


@dataclass(frozen=True)
class HttpClientPolicy:
    user_agent: str
    minimum_interval_seconds: float = 0.1
    max_429_retries: int = 3
    initial_backoff_seconds: float = 0.5
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ValueError("an explicit provider User-Agent is required")
        if self.minimum_interval_seconds < 0 or self.max_429_retries < 0:
            raise ValueError("HTTP rate and retry limits cannot be negative")
        if self.initial_backoff_seconds < 0 or self.timeout_seconds <= 0:
            raise ValueError("HTTP timeout/backoff policy is invalid")


class UrllibTransport:
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                return HttpResponse(
                    status_code=response.status,
                    content=response.read(),
                    headers=dict(response.headers.items()),
                    url=response.geturl(),
                )
        except HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                content=exc.read(),
                headers=dict(exc.headers.items()) if exc.headers else {},
                url=url,
            )


class ResilientHttpClient:
    """Synchronous, bounded primary-source HTTP policy with conditional caching."""

    def __init__(
        self,
        policy: HttpClientPolicy,
        transport: object | None = None,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.policy = policy
        self.transport = transport or UrllibTransport()
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self._last_request_at: float | None = None
        self._cache_headers: dict[str, dict[str, str]] = {}

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
        request_headers = {
            "Accept": "*/*",
            "User-Agent": self.policy.user_agent,
            **self._cache_headers.get(url, {}),
            **dict(headers or {}),
        }
        for attempt in range(self.policy.max_429_retries + 1):
            self._rate_limit()
            try:
                response = self.transport.get(
                    url, headers=request_headers, timeout=self.policy.timeout_seconds
                )
            except Exception as exc:
                raise FeedHttpError(f"primary-source request failed for {url}") from exc
            self._last_request_at = self.monotonic_clock()
            if response.status_code == 429:
                if attempt == self.policy.max_429_retries:
                    raise FeedHttpError(f"primary-source rate limit exhausted for {url}")
                self.sleeper(self.policy.initial_backoff_seconds * (2**attempt))
                continue
            if response.status_code == 304:
                return response
            if not 200 <= response.status_code < 300:
                raise FeedHttpError(
                    f"primary-source HTTP {response.status_code} for {url}"
                )
            conditional: dict[str, str] = {}
            etag = self._header(response.headers, "etag")
            modified = self._header(response.headers, "last-modified")
            if etag:
                conditional["If-None-Match"] = etag
            if modified:
                conditional["If-Modified-Since"] = modified
            if conditional:
                self._cache_headers[url] = conditional
            return response
        raise AssertionError("bounded retry loop exhausted unexpectedly")

    def _rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = (
            self.policy.minimum_interval_seconds
            - (self.monotonic_clock() - self._last_request_at)
        )
        if remaining > 0:
            self.sleeper(remaining)

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        return next((value for key, value in headers.items() if key.lower() == name), None)
