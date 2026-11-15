"""Client for the TubeTranscript REST API (``POST /api/v1/extract``).

Contract: https://tubetranscript.io/docs/api
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests
import requests.auth
from urllib3.exceptions import NewConnectionError

from ._errors import (
    AuthenticationError,
    ConfigurationError,
    ExtractionFailedError,
    ExtractionInProgressError,
    FeaturesFailedError,
    InvalidRequestError,
    PlanRequiredError,
    RateLimitError,
    ServiceUnavailableError,
    TranscriptNotFoundError,
    TubeTranscriptError,
)
from ._version import __version__

logger = logging.getLogger("tubetranscript")

DEFAULT_BASE_URL = "https://tubetranscript.io"
API_KEY_ENV = "TUBETRANSCRIPT_API_KEY"
BASE_URL_ENV = "TUBETRANSCRIPT_BASE_URL"

FEATURES = ("hooks", "mindmap", "action_items")
# The server answers within ~280 s and documents a 300 s client timeout; connecting should take seconds.
CONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 300.0
DEFAULT_RETRY_AFTER_SECONDS = 60.0
# How long transcript() keeps resending a request while audio transcription runs (202).
DEFAULT_WAIT_SECONDS = 600.0
DEFAULT_MAX_RETRIES = 2
# Longest pause between two retries of a transient failure, whatever Retry-After says.
MAX_RETRY_DELAY_SECONDS = 30.0

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Mirrors the API's key format. The API's edge only lets a key of this exact shape skip
# the per-IP guest limit, so any other string would spend the caller's guest quota before failing.
_API_KEY = re.compile(r"^tt_live_[A-Za-z0-9_-]{32}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Refused before the request is counted (the quota store is down): always safe to resend. WORKER_UNAVAILABLE is not
# here: it comes after the request was counted and means minutes of maintenance, so quick retries would only spend
# quota.
_TRANSIENT_CODES = {"RATE_LIMIT_UNAVAILABLE"}
# 502/503 without the API's JSON envelope are the gateway in front of the API (for example during a restart). 504 is
# not here: the API had the request for the gateway's whole timeout.
_GATEWAY_STATUSES = {502, 503}


class _BearerAuth(requests.auth.AuthBase):
    """Sets ``Authorization: Bearer <key>``; never shows the key in ``repr``."""

    def __init__(self, key: str) -> None:
        self._key = key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self._key}"
        return request

    def __repr__(self) -> str:
        return "_BearerAuth(<hidden>)"


def _never_connected(error: requests.RequestException) -> bool:
    """True when no connection to the API was made (DNS failure, refused, connect timeout): nothing was sent."""
    if isinstance(error, requests.ConnectTimeout):
        return True
    reason = getattr(error.args[0], "reason", None) if error.args else None
    return isinstance(reason, NewConnectionError)


@dataclass(frozen=True)
class Chunk:
    """One timed piece of a transcript, in seconds."""

    text: str
    start: float
    duration: float


@dataclass(frozen=True)
class TranscriptResult:
    """A transcript as returned by the API."""

    video_id: str
    """The YouTube video ID; for TikTok, Instagram and Facebook links, an internal key such as ``t:3f9a...``."""
    text: str
    chunks: List[Chunk]
    source: str
    """``captions``, ``audio`` (no captions, so the audio was transcribed), ``media`` (a TikTok, Instagram or
    Facebook link, transcribed) or ``existing_job`` (reused from an earlier request of your team)."""
    platform: str = "youtube"
    language: Optional[str] = None
    video_title: Optional[str] = None
    channel_name: Optional[str] = None
    job_id: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)
    rate_limit_remaining: Optional[int] = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None) -> TranscriptResult:
        """``headers`` should be case-insensitive (``requests`` response headers are)."""
        transcript = data.get("transcript") or {}
        chunks = [
            Chunk(
                text=str(chunk.get("text", "")),
                start=float(chunk.get("start") or 0.0),
                duration=float(chunk.get("duration") or 0.0),
            )
            for chunk in transcript.get("chunks") or []
            if isinstance(chunk, Mapping)
        ]
        remaining = (headers or {}).get("X-RateLimit-Remaining")
        features = data.get("features")
        return cls(
            video_id=str(data.get("video_id") or ""),
            text=str(transcript.get("text") or ""),
            chunks=chunks,
            source=str(data.get("source") or ""),
            platform=str(data.get("platform") or "youtube"),
            language=data.get("language"),
            video_title=data.get("video_title"),
            channel_name=data.get("channel_name"),
            job_id=data.get("job_id"),
            features=dict(features) if isinstance(features, Mapping) else {},
            rate_limit_remaining=int(remaining) if remaining and remaining.isdigit() else None,
        )


def video_url(video_or_url: str) -> str:
    """Turns an 11-character YouTube video ID into a watch URL; anything else is passed through as a URL."""
    value = video_or_url.strip()
    if _VIDEO_ID.match(value):
        return f"https://www.youtube.com/watch?v={value}"
    return value


class TubeTranscript:
    """Client for the TubeTranscript API.

    >>> client = TubeTranscript()  # reads TUBETRANSCRIPT_API_KEY
    >>> result = client.transcript("https://youtu.be/jNQXAC9IVRw")
    >>> print(result.text)

    ``max_retries`` resends a request after a transient failure, with exponential backoff: always when the API cannot
    have counted it (no connection made, ``RATE_LIMIT_UNAVAILABLE``); and, for calls without ``features``, after a
    dropped connection or a 502/503 page from the gateway. Quota refusals (429), ``WORKER_UNAVAILABLE`` and other
    errors are not retried.

    The client uses a ``requests.Session``. Sharing one client between threads works in practice; for strict
    guarantees, use one client per thread.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = (api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")).strip()
        if not key:
            raise ConfigurationError(
                f"No API key. Pass api_key=... or set {API_KEY_ENV}. "
                "Create a key at https://tubetranscript.io/dashboard/api",
                code="MISSING_API_KEY",
            )
        if not _API_KEY.match(key):
            # Never echo the value: it may be a real secret with a typo.
            raise ConfigurationError(
                "The API key is not in the expected format (tt_live_ followed by 32 characters). "
                "Copy it again from https://tubetranscript.io/dashboard/api",
                code="INVALID_API_KEY_FORMAT",
            )
        if max_retries < 0:
            raise ConfigurationError("max_retries must be 0 or more.", code="INVALID_MAX_RETRIES")
        self._api_key = key
        self.base_url = _check_base_url(base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._sleep = sleep

    def __repr__(self) -> str:  # never print the key
        return f"TubeTranscript(base_url={self.base_url!r})"

    def close(self) -> None:
        """Closes the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> TubeTranscript:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def transcript(
        self,
        video: str,
        *,
        features: Sequence[str] = (),
        wait: float = DEFAULT_WAIT_SECONDS,
    ) -> TranscriptResult:
        """Returns the transcript of a YouTube video (ID or URL), or a TikTok, Instagram or Facebook video URL.

        ``features`` asks for AI extras in the same call: any of ``hooks``, ``mindmap``, ``action_items``.

        When the video has no captions the server transcribes the audio, which can take minutes. The server holds
        each request for up to ~280 s and answers 202 if it is still running; the request is then resent after
        ``Retry-After`` until ``wait`` seconds have passed, and :class:`ExtractionInProgressError` is raised. Because a
        request already in flight is not cut short, the call can last up to about ``wait`` + 300 s. Every resend
        counts as one request against the plan's daily limit.
        """
        if isinstance(features, str):
            features = [features]
        unknown = [name for name in features if name not in FEATURES]
        if unknown:
            raise InvalidRequestError(
                f"Unknown feature(s): {', '.join(unknown)}. Use any of: {', '.join(FEATURES)}.",
                code="INVALID_INPUT",
            )
        if not video or not video.strip():
            raise InvalidRequestError("video must be a YouTube video ID or a video URL.", code="INVALID_INPUT")
        body = {"url": video_url(video), "features": list(dict.fromkeys(features))}
        deadline = time.monotonic() + max(wait, 0.0)
        # A request that may have reached the API is resent only without features: features spend AI runs, which a
        # lost response must never charge twice.
        retry_ambiguous = not body["features"]
        while True:
            response = self._send("/api/v1/extract", body, retry_ambiguous=retry_ambiguous)
            payload = _json(response)
            if response.status_code != 202:
                data = payload.get("data")
                if response.status_code == 200 and payload.get("success") is True and isinstance(data, Mapping):
                    try:
                        return TranscriptResult.from_api(data, response.headers)
                    except (TypeError, ValueError) as error:
                        raise TubeTranscriptError(
                            f"The API returned a transcript this client cannot read: {error}",
                            code="INVALID_RESPONSE", status=200,
                        ) from None
                raise _error_from(response, payload)
            retry_after = _retry_after(response) or DEFAULT_RETRY_AFTER_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                code, message, _details = _error_parts(payload)
                data = payload.get("data")
                raise ExtractionInProgressError(
                    message or "Audio transcription is still running. Call again later.",
                    code=code or "EXTRACTION_IN_PROGRESS",
                    status=202,
                    data=data if isinstance(data, dict) else None,
                )
            logger.debug("Transcription still running; asking again in %.0f s", min(retry_after, remaining))
            self._sleep(min(retry_after, remaining))

    def _send(self, path: str, body: Dict[str, Any], *, retry_ambiguous: bool) -> requests.Response:
        """POSTs once, then resends after transient failures (up to ``max_retries`` times).

        Always resent: failures where the API certainly did not count the request (no connection made,
        ``RATE_LIMIT_UNAVAILABLE``). Resent only when ``retry_ambiguous``: failures after the request may have reached
        the API (connection dropped after sending, a 502/503 page from the gateway), which can count one more request.
        """
        attempt = 0
        while True:
            try:
                response = self._post(path, body)
            except ServiceUnavailableError as error:
                retryable = error.code == "CONNECT_ERROR" or (retry_ambiguous and error.code == "NETWORK_ERROR")
                if not retryable or attempt >= self.max_retries:
                    raise
                delay = _backoff(attempt)
                logger.debug("%s; retry %d/%d in %.1f s", error.message, attempt + 1, self.max_retries, delay)
            else:
                if attempt >= self.max_retries or not _is_transient(response, retry_ambiguous=retry_ambiguous):
                    return response
                delay = min(_retry_after(response) or _backoff(attempt), MAX_RETRY_DELAY_SECONDS)
                logger.debug("HTTP %d from the API; retry %d/%d in %.1f s",
                             response.status_code, attempt + 1, self.max_retries, delay)
            attempt += 1
            self._sleep(delay)

    def _post(self, path: str, body: Dict[str, Any]) -> requests.Response:
        url = self.base_url + path
        try:
            response = self._session.post(
                url,
                json=body,
                # Passed as auth (not a header) so a ~/.netrc entry for the host cannot replace it.
                auth=_BearerAuth(self._api_key),
                headers={
                    "User-Agent": f"tubetranscript-python/{__version__}",
                    "Accept": "application/json",
                },
                timeout=(min(CONNECT_TIMEOUT_SECONDS, self.timeout), self.timeout),
                # A redirect would turn the POST into a GET and drop the key; the API never redirects.
                allow_redirects=False,
            )
        except requests.RequestException as error:
            # The exception keeps the prepared request, whose headers hold the key; error trackers that walk
            # the __cause__ chain must not find it.
            error.request = None
            if isinstance(error, requests.ReadTimeout):
                # The server already had the full 300 s: resending would only double the wait. Not retried.
                raise ServiceUnavailableError(
                    f"{self.base_url} did not answer within {self.timeout:.0f} s", code="READ_TIMEOUT"
                ) from error
            if isinstance(error, (requests.ConnectionError, requests.Timeout)):
                code = "CONNECT_ERROR" if _never_connected(error) else "NETWORK_ERROR"
                raise ServiceUnavailableError(
                    f"Could not reach {self.base_url}: {type(error).__name__}", code=code
                ) from error
            raise TubeTranscriptError(
                f"Request to {self.base_url} failed: {type(error).__name__}", code="REQUEST_ERROR"
            ) from error
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            raise ConfigurationError(
                f"{url} redirects to {location or 'another address'}. "
                f"Set base_url to the final address (the default is {DEFAULT_BASE_URL}).",
                code="UNEXPECTED_REDIRECT",
                status=response.status_code,
            )
        return response


def _check_base_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConfigurationError(f"Invalid base URL: {url!r}", code="INVALID_BASE_URL")
    # Never send the key in clear text to a remote host.
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ConfigurationError("The base URL must use https (http is allowed for localhost only).",
                                 code="INVALID_BASE_URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("The base URL must not contain credentials, a query or a fragment.",
                                 code="INVALID_BASE_URL")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _json(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _retry_after(response: requests.Response) -> Optional[float]:
    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None  # NaN fails the comparison too


def _backoff(attempt: int) -> float:
    """1 s, 2 s, 4 s... with up to 25% jitter, so many clients do not retry in step."""
    base: float = min(2.0 ** attempt, MAX_RETRY_DELAY_SECONDS)
    return base * (1 + random.random() * 0.25)  # noqa: S311 (not for security)


def _is_api_body(payload: Mapping[str, Any]) -> bool:
    """True for a body the API (or the edge limit in front of it) wrote, as opposed to a proxy or framework page."""
    return "success" in payload or isinstance(payload.get("error"), str)


def _is_transient(response: requests.Response, *, retry_ambiguous: bool) -> bool:
    payload = _json(response)
    code, _message, _details = _error_parts(payload)
    if code in _TRANSIENT_CODES:
        return True
    return retry_ambiguous and response.status_code in _GATEWAY_STATUSES and not _is_api_body(payload)


def _error_parts(payload: Mapping[str, Any]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Code, message and details of an error body.

    The API answers ``{"success": false, "error": {"code", "message", "details"}}``. A few responses from in front of
    the API (the per-IP guest limit) use ``{"error": "<CODE>", "message": "..."}`` instead.
    """
    error = payload.get("error")
    if isinstance(error, Mapping):
        details = error.get("details")
        parsed = dict(details) if isinstance(details, Mapping) else None
        return str(error.get("code") or ""), str(error.get("message") or ""), parsed
    if isinstance(error, str):
        return error, str(payload.get("message") or ""), None
    return "", "", None


_BY_STATUS = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: PlanRequiredError,
    404: TranscriptNotFoundError,
    422: ExtractionFailedError,
    502: FeaturesFailedError,
}


def _error_from(response: requests.Response, payload: Dict[str, Any]) -> TubeTranscriptError:
    status = response.status_code
    code, message, details = _error_parts(payload)
    data = payload.get("data")
    kwargs: Dict[str, Any] = {
        "code": code or f"HTTP_{status}",
        "status": status,
        "details": details,
        "data": data if isinstance(data, dict) else None,
    }
    message = message or response.reason or "Request failed."
    if 400 <= status < 500 and not _is_api_body(payload):
        # A 4xx page that is not the API's (for example a 404 page because base_url has an extra path).
        kwargs["code"] = "UNEXPECTED_RESPONSE"
        return TubeTranscriptError(
            f"HTTP {status} from {getattr(response, 'url', '') or 'the API'} without an API response body. "
            "Check base_url.",
            **kwargs,
        )
    # 429: an allowance is used up. 409 BUSY: another AI run of the team is in progress. Both mean "try later".
    if status in (409, 429):
        return RateLimitError(message, retry_after=_retry_after(response), **kwargs)
    # A 502 is FEATURES_FAILED only when the API says so; a proxy's 502 page is an outage.
    if status in _BY_STATUS and (status != 502 or kwargs["code"] == "FEATURES_FAILED"):
        return _BY_STATUS[status](message, **kwargs)
    if status >= 500:
        return ServiceUnavailableError(message, **kwargs)
    return TubeTranscriptError(message, **kwargs)


def iter_features(names: Iterable[str]) -> List[str]:
    """Splits a comma-separated feature list (CLI helper)."""
    return [part.strip() for name in names for part in name.split(",") if part.strip()]
