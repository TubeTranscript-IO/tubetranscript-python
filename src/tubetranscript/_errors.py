"""Errors raised by the TubeTranscript client.

Every error carries the API's ``code`` (for example ``RATE_LIMIT_EXCEEDED``), the HTTP ``status`` and, when the API
returned one, a partial ``data`` payload: a refused AI feature still comes with the transcript.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class TubeTranscriptError(Exception):
    """Base class for every error this package raises."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "UNKNOWN_ERROR",
        status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}
        self.data = data

    def __str__(self) -> str:
        prefix = f"[{self.status} {self.code}]" if self.status else f"[{self.code}]"
        return f"{prefix} {self.message}"


class ConfigurationError(TubeTranscriptError):
    """The client is missing an API key or has an unusable base URL."""


class AuthenticationError(TubeTranscriptError):
    """401: the API key is missing, unknown or revoked."""


class PlanRequiredError(TubeTranscriptError):
    """403: the key's team is not on a plan that includes the API."""


class InvalidRequestError(TubeTranscriptError):
    """400: the URL or the requested features were rejected."""


class TranscriptNotFoundError(TubeTranscriptError):
    """404: the video is unavailable."""


class ExtractionFailedError(TubeTranscriptError):
    """422: the video could not be transcribed (private, removed, no speech...)."""


class RateLimitError(TubeTranscriptError):
    """429 (an allowance is used up) or 409 ``BUSY`` (another AI run of the team is in progress).

    ``retry_after`` is in seconds when the API sent it (not for ``WHISPER_LIMIT``/``MEDIA_LIMIT``). When the refusal
    is about AI features, ``data`` still holds the transcript.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ExtractionInProgressError(TubeTranscriptError):
    """The video has no captions and audio transcription was still running when the wait ran out.

    Call again with the same URL later: the server resumes the same job, nothing is transcribed twice.
    """

    @property
    def job_id(self) -> Optional[str]:
        return (self.data or {}).get("job_id")


class FeaturesFailedError(TubeTranscriptError):
    """502: some AI features failed. ``data`` still holds the transcript and the features that succeeded."""


class ServiceUnavailableError(TubeTranscriptError):
    """5xx or a network failure: retry later."""
