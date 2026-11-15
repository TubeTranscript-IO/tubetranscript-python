from __future__ import annotations

import warnings

import pytest
from youtube_transcript_api import (
    FailedToCreateConsentCookie,
    VideoUnavailable,
    YouTubeDataUnparsable,
    FetchedTranscript,
    FetchedTranscriptSnippet,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
)
from youtube_transcript_api import YouTubeTranscriptApi as UpstreamApi
from youtube_transcript_api.formatters import SRTFormatter

import tubetranscript._compat as compat
from conftest import ENV_KEY, KEY, FakeResponse, FakeSession, error_body, ok_body
from tubetranscript import (
    AuthenticationError,
    ConfigurationError,
    TubeTranscript,
    YouTubeTranscriptApi,
)

VIDEO = "jNQXAC9IVRw"


def direct_ok():
    return FetchedTranscript(
        snippets=[FetchedTranscriptSnippet(text="direct", start=0.0, duration=1.0)],
        video_id=VIDEO, language="English", language_code="en", is_generated=False,
    )


def patch_direct(monkeypatch, outcome):
    calls = []

    def fake_fetch(self, video_id, languages=("en",), preserve_formatting=False):
        calls.append((video_id, tuple(languages), preserve_formatting))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(UpstreamApi, "fetch", fake_fetch)
    return calls


def api(*responses, **kwargs):
    session = FakeSession(*responses)
    client = TubeTranscript(KEY, session=session, sleep=lambda _s: None)
    return YouTubeTranscriptApi(client=client, **kwargs), session


@pytest.fixture(autouse=True)
def _reset_warning():
    compat._warned_missing_key = False


def test_is_a_subclass_of_upstream():
    assert issubclass(YouTubeTranscriptApi, UpstreamApi)


def test_direct_success_never_calls_api(monkeypatch):
    calls = patch_direct(monkeypatch, direct_ok())
    ytt, session = api()
    fetched = ytt.fetch(VIDEO, languages=["de", "en"], preserve_formatting=True)
    assert fetched[0].text == "direct"
    assert calls == [(VIDEO, ("de", "en"), True)]
    assert session.calls == []


@pytest.mark.parametrize("blocked", [
    RequestBlocked(VIDEO), IpBlocked(VIDEO), PoTokenRequired(VIDEO),
    FailedToCreateConsentCookie(VIDEO), YouTubeDataUnparsable(VIDEO),
])
def test_blocked_falls_back_to_api(monkeypatch, blocked):
    patch_direct(monkeypatch, blocked)
    ytt, session = api(FakeResponse(200, ok_body()))
    fetched = ytt.fetch(VIDEO)
    assert isinstance(fetched, FetchedTranscript)
    assert fetched.video_id == VIDEO
    assert fetched.language_code == "en"
    assert fetched.is_generated is False
    assert [s.text for s in fetched] == ["All right, so here we are", "in front of the elephants"]
    assert fetched.to_raw_data()[0] == {"text": "All right, so here we are", "start": 1.2, "duration": 2.16}
    assert session.calls[0]["json"]["url"] == f"https://www.youtube.com/watch?v={VIDEO}"
    # Upstream formatters accept the result.
    assert SRTFormatter().format_transcript(fetched).startswith("1\n00:00:01,200 --> ")


def test_no_captions_raises_by_default(monkeypatch):
    patch_direct(monkeypatch, TranscriptsDisabled(VIDEO))
    ytt, session = api()
    with pytest.raises(TranscriptsDisabled):
        ytt.fetch(VIDEO)
    assert session.calls == []


def test_no_captions_falls_back_when_opted_in(monkeypatch):
    patch_direct(monkeypatch, TranscriptsDisabled(VIDEO))
    ytt, _ = api(FakeResponse(200, ok_body(source="audio", language=None)), fallback_on_no_captions=True)
    fetched = ytt.fetch(VIDEO)
    assert fetched.is_generated is True
    assert fetched.language_code == ""
    assert fetched.language == "unknown"


def test_language_mismatch_is_not_a_fallback(monkeypatch):
    patch_direct(monkeypatch, NoTranscriptFound(VIDEO, ["de"], None))
    ytt, session = api(fallback_on_no_captions=True)
    with pytest.raises(NoTranscriptFound):
        ytt.fetch(VIDEO, languages=["de"])
    assert session.calls == []


def test_without_key_reraises_original_with_one_warning(monkeypatch):
    patch_direct(monkeypatch, RequestBlocked(VIDEO))
    ytt = YouTubeTranscriptApi()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RequestBlocked):
            ytt.fetch(VIDEO)
        with pytest.raises(RequestBlocked):
            ytt.fetch(VIDEO)
    hints = [w for w in caught if "TUBETRANSCRIPT_API_KEY" in str(w.message)]
    assert len(hints) == 1


def test_api_error_is_chained_to_block(monkeypatch):
    patch_direct(monkeypatch, IpBlocked(VIDEO))
    ytt, _ = api(FakeResponse(401, error_body("INVALID_API_KEY")))
    with pytest.raises(AuthenticationError) as info:
        ytt.fetch(VIDEO)
    assert isinstance(info.value.__cause__, IpBlocked)


def test_video_errors_are_not_fallbacks(monkeypatch):
    patch_direct(monkeypatch, VideoUnavailable(VIDEO))
    ytt, session = api(fallback_on_no_captions=True)
    with pytest.raises(VideoUnavailable):
        ytt.fetch(VIDEO)
    assert session.calls == []


def test_bad_base_url_is_chained_to_block(monkeypatch):
    patch_direct(monkeypatch, IpBlocked(VIDEO))
    ytt = YouTubeTranscriptApi(api_key=KEY, base_url="http://example.com")
    with pytest.raises(ConfigurationError) as info:
        ytt.fetch(VIDEO)
    assert isinstance(info.value.__cause__, IpBlocked)


def test_wait_is_passed_to_client(monkeypatch):
    patch_direct(monkeypatch, IpBlocked(VIDEO))
    pending = FakeResponse(202, error_body("EXTRACTION_IN_PROGRESS", data={"job_id": "j9", "status": "queued"}))
    ytt, session = api(pending, wait=0)
    from tubetranscript import ExtractionInProgressError
    with pytest.raises(ExtractionInProgressError):
        ytt.fetch(VIDEO)
    assert len(session.calls) == 1


def test_skip_direct(monkeypatch):
    calls = patch_direct(monkeypatch, direct_ok())
    ytt, session = api(FakeResponse(200, ok_body()), try_direct_first=False)
    assert ytt.fetch(VIDEO)[0].text == "All right, so here we are"
    assert calls == []
    assert len(session.calls) == 1


def test_skip_direct_without_key_is_a_configuration_error(monkeypatch):
    patch_direct(monkeypatch, direct_ok())
    with pytest.raises(ConfigurationError):
        YouTubeTranscriptApi(try_direct_first=False).fetch(VIDEO)


def test_key_from_env_is_used(monkeypatch):
    patch_direct(monkeypatch, RequestBlocked(VIDEO))
    monkeypatch.setenv("TUBETRANSCRIPT_API_KEY", ENV_KEY)
    session = FakeSession(FakeResponse(200, ok_body()))
    monkeypatch.setattr(compat, "TubeTranscript", lambda key, base_url=None: TubeTranscript(key, base_url=base_url, session=session))
    YouTubeTranscriptApi().fetch(VIDEO)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer " + ENV_KEY
