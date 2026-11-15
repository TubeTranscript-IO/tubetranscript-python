"""YouTube transcripts that work from cloud servers, including videos without captions.

Two ways to use it:

- ``from tubetranscript import YouTubeTranscriptApi``: a drop-in for youtube-transcript-api whose ``fetch()``
  falls back to the TubeTranscript API when YouTube blocks your IP.
- ``TubeTranscript().transcript(url)``: the TubeTranscript API directly (YouTube, TikTok, Instagram, Facebook).
"""

import logging

from ._version import __version__
from ._client import Chunk, TranscriptResult, TubeTranscript
from ._compat import YouTubeTranscriptApi, to_fetched_transcript
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

# Library convention: log only when the application configures logging (logging.getLogger("tubetranscript")).
logging.getLogger("tubetranscript").addHandler(logging.NullHandler())

__all__ = [  # noqa: RUF022 (dunder first, then classes, then functions)
    "__version__",
    "AuthenticationError",
    "Chunk",
    "ConfigurationError",
    "ExtractionFailedError",
    "ExtractionInProgressError",
    "FeaturesFailedError",
    "InvalidRequestError",
    "PlanRequiredError",
    "RateLimitError",
    "ServiceUnavailableError",
    "TranscriptNotFoundError",
    "TranscriptResult",
    "TubeTranscript",
    "TubeTranscriptError",
    "YouTubeTranscriptApi",
    "to_fetched_transcript",
]
