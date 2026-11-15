"""Drop-in replacement for ``youtube_transcript_api.YouTubeTranscriptApi``.

Change one import and ``fetch()`` keeps working when YouTube blocks your server's IP (common on AWS, GCP, Azure,
Vercel and most VPS providers)::

    # from youtube_transcript_api import YouTubeTranscriptApi
    from tubetranscript import YouTubeTranscriptApi

    fetched = YouTubeTranscriptApi().fetch("jNQXAC9IVRw")

The direct fetch runs first, exactly as in youtube-transcript-api. Only when it fails because the request was
blocked (and, if you opt in, because the video has no captions) does the call go to the TubeTranscript API, using
the key in ``TUBETRANSCRIPT_API_KEY``. The result is a regular ``FetchedTranscript``, so formatters and code that
read ``snippet.text``/``snippet.start`` keep working.
"""

from __future__ import annotations

import warnings
from typing import Iterable, Optional, Tuple, Type

from requests import Session
from youtube_transcript_api import (
    FailedToCreateConsentCookie,
    FetchedTranscript,
    FetchedTranscriptSnippet,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)
from youtube_transcript_api import YouTubeTranscriptApi as _UpstreamApi
from youtube_transcript_api.proxies import ProxyConfig

from ._client import API_KEY_ENV, DEFAULT_WAIT_SECONDS, TranscriptResult, TubeTranscript, logger
from ._errors import ConfigurationError, TubeTranscriptError

# Failures that come from YouTube refusing or confusing this machine (IP blocks including IpBlocked, PO token, consent
# or bot pages, any other HTTP error from YouTube), not from the video itself. Video-level errors (TranscriptsDisabled,
# NoTranscriptFound, VideoUnavailable, AgeRestricted, InvalidVideoId...) are re-raised unchanged.
BLOCKED_ERRORS: Tuple[Type[Exception], ...] = (
    RequestBlocked,
    PoTokenRequired,
    FailedToCreateConsentCookie,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)

_warned_missing_key = False


class YouTubeTranscriptApi(_UpstreamApi):
    """``youtube_transcript_api.YouTubeTranscriptApi`` with a TubeTranscript fallback for ``fetch()``.

    Extra keyword arguments (all optional):

    - ``api_key``: TubeTranscript API key; defaults to the ``TUBETRANSCRIPT_API_KEY`` environment variable.
      Without a key, the class behaves exactly like the original.
    - ``fallback_on_no_captions``: also fall back when the video has captions disabled. The API then transcribes
      the audio, which uses your plan's audio allowance and can take minutes. Default ``False``.
    - ``try_direct_first``: set ``False`` to skip the direct request and always use the API (saves the wasted
      round trip on servers YouTube always blocks). Default ``True``.
    - ``client``: a ready :class:`tubetranscript.TubeTranscript` instance (overrides ``api_key``/``base_url``).
    - ``wait``: seconds to keep resending while the API transcribes audio (see ``TubeTranscript.transcript``).

    Only ``fetch()`` falls back. ``list()`` and translation still talk to YouTube directly.

    Note: once a call reaches the API, the API itself transcribes the audio if the video turns out to have no
    captions (it has no captions-only mode). ``fallback_on_no_captions`` only decides whether a direct
    ``TranscriptsDisabled`` result is sent to the API at all.
    """

    def __init__(
        self,
        proxy_config: Optional[ProxyConfig] = None,
        http_client: Optional[Session] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        fallback_on_no_captions: bool = False,
        try_direct_first: bool = True,
        client: Optional[TubeTranscript] = None,
        wait: float = DEFAULT_WAIT_SECONDS,
    ) -> None:
        super().__init__(proxy_config=proxy_config, http_client=http_client)
        self._client = client
        self._wait = wait
        self._api_key = api_key
        self._base_url = base_url
        self._fallback_on_no_captions = fallback_on_no_captions
        self._try_direct_first = try_direct_first

    def fetch(
        self,
        video_id: str,
        languages: Iterable[str] = ("en",),
        preserve_formatting: bool = False,
    ) -> FetchedTranscript:
        """Fetches a transcript; see the class docstring for when the TubeTranscript API is used.

        On the fallback path ``languages`` and ``preserve_formatting`` are not applied: the API returns the video's
        default transcript language, reported in ``FetchedTranscript.language_code``.
        """
        if not self._try_direct_first:
            return self._fetch_from_api(video_id, cause=None)
        try:
            return super().fetch(video_id, languages=languages, preserve_formatting=preserve_formatting)
        except BLOCKED_ERRORS as error:
            cause: Exception = error
        except TranscriptsDisabled as error:
            if not self._fallback_on_no_captions:
                raise
            cause = error
        return self._fetch_from_api(video_id, cause=cause)

    def _fetch_from_api(self, video_id: str, cause: Optional[Exception]) -> FetchedTranscript:
        try:
            client = self._get_client()
        except ConfigurationError as error:
            if cause is not None:
                raise error from cause
            raise
        if client is None:
            if cause is None:
                raise ConfigurationError(
                    f"try_direct_first=False needs an API key: pass api_key=... or set {API_KEY_ENV}.",
                    code="MISSING_API_KEY",
                )
            _warn_missing_key()
            raise cause
        if cause is not None:
            logger.debug("Direct fetch of %s failed with %s; using the TubeTranscript API",
                         video_id, type(cause).__name__)
        try:
            result = client.transcript(video_id, wait=self._wait)
        except TubeTranscriptError as error:
            if cause is not None:
                raise error from cause
            raise
        return to_fetched_transcript(result, video_id)

    def _get_client(self) -> Optional[TubeTranscript]:
        if self._client is None:
            try:
                self._client = TubeTranscript(self._api_key, base_url=self._base_url)
            except ConfigurationError as error:
                if error.code == "MISSING_API_KEY":
                    return None
                raise
        return self._client


def to_fetched_transcript(result: TranscriptResult, video_id: str) -> FetchedTranscript:
    """Converts an API result into youtube-transcript-api's ``FetchedTranscript``.

    ``is_generated`` is ``True`` when the API transcribed the audio (``audio`` and ``media`` sources). For captions
    the API does not say whether they were uploaded or auto-generated, so it is ``False``; the same for
    ``existing_job``, whose origin the API does not report.
    """
    language = result.language or ""
    return FetchedTranscript(
        snippets=[FetchedTranscriptSnippet(text=c.text, start=c.start, duration=c.duration) for c in result.chunks],
        video_id=result.video_id or video_id,
        language=language or "unknown",
        language_code=language,
        is_generated=result.source in ("audio", "media"),
    )


def _warn_missing_key() -> None:
    global _warned_missing_key
    if _warned_missing_key:
        return
    _warned_missing_key = True
    warnings.warn(
        f"YouTube blocked this request. Set {API_KEY_ENV} to fall back to the TubeTranscript API "
        "(https://tubetranscript.io/youtube-transcript-api).",
        RuntimeWarning,
        stacklevel=4,
    )
