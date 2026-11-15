"""Release hardening: behaviour against the real shapes production returns (2026-11-15 review)."""

from __future__ import annotations

import logging

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError
from youtube_transcript_api import IpBlocked
from youtube_transcript_api import YouTubeTranscriptApi as UpstreamApi

from conftest import KEY, FakeResponse, FakeSession, error_body, ok_body
from tubetranscript import (
    ConfigurationError,
    InvalidRequestError,
    RateLimitError,
    ServiceUnavailableError,
    TubeTranscript,
    TubeTranscriptError,
    YouTubeTranscriptApi,
)
from tubetranscript._cli import main
from tubetranscript._client import TranscriptResult, _BearerAuth

VIDEO = "jNQXAC9IVRw"


def make(*responses, max_retries=2):
    session = FakeSession(*responses)
    sleeps = []
    client = TubeTranscript(KEY, session=session, sleep=sleeps.append, max_retries=max_retries)
    return client, session, sleeps


# --- API key format -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "tt_live_short",
    "tt_test_" + "A" * 32,
    "tt_live_" + "A" * 31 + "!",
    "tt_live_" + "A" * 16 + "\n" + "A" * 15,
    "sk-" + "A" * 40,
])
def test_malformed_key_is_rejected_without_a_request(key):
    with pytest.raises(ConfigurationError) as info:
        TubeTranscript(key, session=FakeSession())
    assert info.value.code == "INVALID_API_KEY_FORMAT"
    assert key.strip() not in str(info.value)


def test_key_surrounding_whitespace_is_trimmed():
    session = FakeSession(FakeResponse(200, ok_body()))
    TubeTranscript(f"  {KEY}\n", session=session).transcript(VIDEO)
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"


# --- Response shapes ------------------------------------------------------------------------------------------------

def test_edge_guest_limit_body_is_parsed_not_crashed():
    # The per-IP guest limit in front of the API answers a 429 with {"error": "<CODE>", "message"} (no envelope).
    body = {"error": "RATE_LIMIT_EXCEEDED", "message": "You have reached the limit of 5 free transcripts per 24 hours.",
            "upgrade_url": "/pricing?source=rate_limit"}
    client, _, _ = make(FakeResponse(429, body, {"Retry-After": "3600"}))
    with pytest.raises(RateLimitError) as info:
        client.transcript(VIDEO)
    assert info.value.code == "RATE_LIMIT_EXCEEDED"
    assert "5 free transcripts" in info.value.message
    assert info.value.retry_after == 3600


@pytest.mark.parametrize("body", [[], "oops", {"success": True, "data": None}, {"success": True, "data": "x"}])
def test_odd_200_bodies_raise_a_library_error(body):
    client, _, _ = make(FakeResponse(200, body))
    with pytest.raises(TubeTranscriptError):
        client.transcript(VIDEO)


def test_malformed_chunks_and_features_are_tolerated():
    data = ok_body()["data"]
    data["transcript"]["chunks"] = [{"text": "ok", "start": None, "duration": "1.5"}, "garbage"]
    data["features"] = ["not", "a", "dict"]
    result = TranscriptResult.from_api(data)
    assert [(c.text, c.start, c.duration) for c in result.chunks] == [("ok", 0.0, 1.5)]
    assert result.features == {}


# --- Redirects ------------------------------------------------------------------------------------------------------

def test_redirect_is_never_followed():
    client, session, _ = make(FakeResponse(301, None, {"Location": "https://tubetranscript.io/api/v1/extract"}))
    with pytest.raises(ConfigurationError) as info:
        client.transcript(VIDEO)
    assert info.value.code == "UNEXPECTED_REDIRECT"
    assert "https://tubetranscript.io/api/v1/extract" in info.value.message
    assert session.calls[0]["allow_redirects"] is False
    assert len(session.calls) == 1


@pytest.mark.parametrize("url", ["https://user:pass@tubetranscript.io", "https://tubetranscript.io/?x=1",
                                 "https://tubetranscript.io/#f"])
def test_base_url_with_credentials_query_or_fragment_rejected(url):
    with pytest.raises(ConfigurationError):
        TubeTranscript(KEY, base_url=url)


# --- Retries --------------------------------------------------------------------------------------------------------

def never_connected():
    """What requests raises when DNS fails or the connection is refused: nothing reached the API."""
    return requests.ConnectionError(MaxRetryError(None, "/api/v1/extract", reason=NewConnectionError(None, "refused")))


@pytest.mark.parametrize("first", [
    FakeResponse(503, error_body("RATE_LIMIT_UNAVAILABLE")),
    FakeResponse(502, None),
    FakeResponse(503, "<html>Service Unavailable</html>"),
    requests.ConnectionError("reset"),
    requests.ConnectTimeout("connect"),
    never_connected(),
])
def test_transient_failures_are_retried(first):
    client, session, sleeps = make(first, FakeResponse(200, ok_body()))
    assert client.transcript(VIDEO).video_id == VIDEO
    assert len(session.calls) == 2
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 30


@pytest.mark.parametrize("first", [
    FakeResponse(503, error_body("RATE_LIMIT_UNAVAILABLE")),
    requests.ConnectTimeout("connect"),
    never_connected(),
])
def test_with_features_only_uncounted_failures_are_retried(first):
    client, session, _ = make(first, FakeResponse(200, ok_body(features={"hooks": []})))
    client.transcript(VIDEO, features=["hooks"])
    assert len(session.calls) == 2


@pytest.mark.parametrize("first", [
    FakeResponse(502, None),
    FakeResponse(503, "<html>Service Unavailable</html>"),
    requests.ConnectionError("reset after send"),
])
def test_with_features_ambiguous_failures_are_not_retried(first):
    # The API may have charged AI runs already; a resend could charge them twice.
    client, session, sleeps = make(first)
    with pytest.raises(ServiceUnavailableError):
        client.transcript(VIDEO, features=["hooks"])
    assert len(session.calls) == 1 and sleeps == []


@pytest.mark.parametrize("response", [
    FakeResponse(429, error_body("RATE_LIMIT_EXCEEDED"), {"Retry-After": "5"}),
    FakeResponse(503, error_body("WORKER_UNAVAILABLE")),
    FakeResponse(504, "<html>Gateway Timeout</html>"),
    FakeResponse(500, error_body("INTERNAL_ERROR")),
    FakeResponse(503, error_body("AI_DISABLED", data=ok_body()["data"])),
    FakeResponse(502, error_body("FEATURES_FAILED", data=ok_body()["data"])),
    FakeResponse(401, error_body("INVALID_API_KEY")),
])
def test_non_transient_failures_are_not_retried(response):
    client, session, sleeps = make(response)
    with pytest.raises(TubeTranscriptError):
        client.transcript(VIDEO)
    assert len(session.calls) == 1 and sleeps == []


def test_read_timeout_is_not_retried():
    client, session, _ = make(requests.ReadTimeout("slow"))
    with pytest.raises(ServiceUnavailableError) as info:
        client.transcript(VIDEO)
    assert info.value.code == "READ_TIMEOUT"
    assert len(session.calls) == 1


def test_retries_give_up_with_the_last_error():
    busy = FakeResponse(503, error_body("RATE_LIMIT_UNAVAILABLE"))
    client, session, sleeps = make(busy, busy, busy)
    with pytest.raises(ServiceUnavailableError) as info:
        client.transcript(VIDEO)
    assert info.value.code == "RATE_LIMIT_UNAVAILABLE"
    assert len(session.calls) == 3 and len(sleeps) == 2


def test_retry_after_is_capped():
    client, _, sleeps = make(FakeResponse(503, error_body("RATE_LIMIT_UNAVAILABLE"), {"Retry-After": "9999"}),
                             FakeResponse(200, ok_body()))
    client.transcript(VIDEO)
    assert sleeps == [30.0]


def test_max_retries_zero_disables_retries():
    client, session, _ = make(requests.ConnectionError("reset"), max_retries=0)
    with pytest.raises(ServiceUnavailableError):
        client.transcript(VIDEO)
    assert len(session.calls) == 1


def test_negative_max_retries_rejected():
    with pytest.raises(ConfigurationError):
        TubeTranscript(KEY, max_retries=-1)


def test_other_request_errors_are_library_errors():
    client, _, _ = make(requests.exceptions.InvalidURL("bad"))
    with pytest.raises(TubeTranscriptError) as info:
        client.transcript(VIDEO)
    assert info.value.code == "REQUEST_ERROR"


# --- Inputs ---------------------------------------------------------------------------------------------------------

def test_single_feature_string_is_accepted():
    client, session, _ = make(FakeResponse(200, ok_body()))
    client.transcript(VIDEO, features="hooks")
    assert session.calls[0]["json"]["features"] == ["hooks"]


@pytest.mark.parametrize("video", ["", "   "])
def test_empty_video_rejected(video):
    client, session, _ = make()
    with pytest.raises(InvalidRequestError):
        client.transcript(video)
    assert session.calls == []


# --- Drop-in --------------------------------------------------------------------------------------------------------

def test_media_source_counts_as_generated(monkeypatch):
    monkeypatch.setattr(UpstreamApi, "fetch", lambda *a, **k: (_ for _ in ()).throw(IpBlocked(VIDEO)))
    client, _, _ = make(FakeResponse(200, ok_body(source="media")))
    assert YouTubeTranscriptApi(client=client).fetch(VIDEO).is_generated is True


def test_fallback_is_logged_without_the_key(monkeypatch, caplog):
    monkeypatch.setattr(UpstreamApi, "fetch", lambda *a, **k: (_ for _ in ()).throw(IpBlocked(VIDEO)))
    client, _, _ = make(FakeResponse(503, error_body("RATE_LIMIT_UNAVAILABLE")), FakeResponse(200, ok_body()))
    with caplog.at_level(logging.DEBUG, logger="tubetranscript"):
        YouTubeTranscriptApi(client=client).fetch(VIDEO)
    text = caplog.text
    assert "IpBlocked" in text and "retry 1/2" in text
    assert KEY not in text


def test_bad_key_format_in_dropin_is_chained_to_block(monkeypatch):
    monkeypatch.setattr(UpstreamApi, "fetch", lambda *a, **k: (_ for _ in ()).throw(IpBlocked(VIDEO)))
    with pytest.raises(ConfigurationError) as info:
        YouTubeTranscriptApi(api_key="tt_live_typo").fetch(VIDEO)
    assert info.value.code == "INVALID_API_KEY_FORMAT"
    assert isinstance(info.value.__cause__, IpBlocked)


def test_cli_rejects_malformed_env_key(monkeypatch, capsys):
    monkeypatch.setenv("TUBETRANSCRIPT_API_KEY", "nope")
    assert main([VIDEO, "-v"]) == 1
    err = capsys.readouterr().err
    assert "INVALID_API_KEY_FORMAT" in err and "nope" not in err.replace("INVALID", "")


# --- Second review round ----------------------------------------------------------------------------------------------

def test_key_is_sent_as_auth_so_netrc_cannot_override_it():
    client, session, _ = make(FakeResponse(200, ok_body()))
    client.transcript(VIDEO)
    call = session.calls[0]
    assert call["auth"] is not None and KEY not in repr(call["auth"])
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"


def test_real_session_ignores_netrc(tmp_path, monkeypatch):
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine tubetranscript.io login someone password other-secret\n")
    monkeypatch.setenv("NETRC", str(netrc))
    session = requests.Session()
    prepared = session.prepare_request(requests.Request(
        "POST", "https://tubetranscript.io/api/v1/extract", auth=_BearerAuth(KEY), json={}))
    assert prepared.headers["Authorization"] == f"Bearer {KEY}"


def test_chained_request_exception_does_not_carry_the_key():
    error = requests.ConnectionError("reset")
    error.request = requests.Request("POST", "https://x", headers={"Authorization": f"Bearer {KEY}"}).prepare()
    client, _, _ = make(error, max_retries=0)
    with pytest.raises(ServiceUnavailableError) as info:
        client.transcript(VIDEO)
    cause = info.value.__cause__
    assert cause is error and cause.request is None


def test_non_api_4xx_page_points_at_base_url():
    client, _, _ = make(FakeResponse(404, "<html>Not found</html>"))
    with pytest.raises(TubeTranscriptError) as info:
        client.transcript(VIDEO)
    assert type(info.value) is TubeTranscriptError
    assert info.value.code == "UNEXPECTED_RESPONSE" and "base_url" in info.value.message


def test_api_404_is_still_transcript_not_found():
    from tubetranscript import TranscriptNotFoundError
    client, _, _ = make(FakeResponse(404, error_body("TRANSCRIPT_NOT_FOUND")))
    with pytest.raises(TranscriptNotFoundError):
        client.transcript(VIDEO)


def test_unreadable_chunk_numbers_raise_a_library_error():
    data = ok_body()["data"]
    data["transcript"]["chunks"] = [{"text": "x", "start": "soon", "duration": 1}]
    client, _, _ = make(FakeResponse(200, {"success": True, "data": data, "error": None}))
    with pytest.raises(TubeTranscriptError) as info:
        client.transcript(VIDEO)
    assert info.value.code == "INVALID_RESPONSE"
