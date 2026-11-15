from __future__ import annotations

import pytest
import requests

from conftest import ENV_KEY, KEY, FakeResponse, FakeSession, error_body, ok_body
from tubetranscript import (
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
    TubeTranscript,
)
from tubetranscript._cli import main, to_srt


def make(*responses, sleeps=None, **kwargs):
    session = FakeSession(*responses)
    recorder = sleeps if sleeps is not None else []
    kwargs.setdefault("max_retries", 0)  # these tests check error mapping; retries are in test_hardening.py
    client = TubeTranscript(KEY, session=session, sleep=recorder.append, **kwargs)
    return client, session


def test_missing_key_raises_configuration_error():
    with pytest.raises(ConfigurationError) as info:
        TubeTranscript()
    assert info.value.code == "MISSING_API_KEY"


def test_key_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("TUBETRANSCRIPT_API_KEY", ENV_KEY)
    monkeypatch.setenv("TUBETRANSCRIPT_BASE_URL", "http://localhost:3000/")
    session = FakeSession(FakeResponse(200, ok_body()))
    TubeTranscript(session=session).transcript("jNQXAC9IVRw")
    call = session.calls[0]
    assert call["url"] == "http://localhost:3000/api/v1/extract"
    assert call["headers"]["Authorization"] == "Bearer " + ENV_KEY


@pytest.mark.parametrize("url", ["http://example.com", "ftp://example.com", "not a url"])
def test_rejects_unsafe_base_url(url):
    with pytest.raises(ConfigurationError):
        TubeTranscript(KEY, base_url=url)


def test_repr_hides_key():
    client, _ = make()
    assert KEY not in repr(client)


def test_success_with_video_id_builds_watch_url():
    client, session = make(FakeResponse(200, ok_body(), {"x-ratelimit-remaining": "99"}))
    result = client.transcript("jNQXAC9IVRw")
    call = session.calls[0]
    assert call["url"] == "https://tubetranscript.io/api/v1/extract"
    assert call["json"] == {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "features": []}
    assert call["timeout"] == (10.0, 300.0)
    assert call["headers"]["User-Agent"].startswith("tubetranscript-python/")
    assert result.video_id == "jNQXAC9IVRw"
    assert result.source == "captions"
    assert result.language == "en"
    assert len(result.chunks) == 2 and result.chunks[0].start == 1.2
    assert result.rate_limit_remaining == 99


def test_urls_pass_through_and_features_dedupe():
    client, session = make(FakeResponse(200, ok_body(features={"hooks": []})))
    client.transcript("https://www.tiktok.com/@a/video/1", features=["hooks", "hooks", "mindmap"])
    assert session.calls[0]["json"] == {"url": "https://www.tiktok.com/@a/video/1", "features": ["hooks", "mindmap"]}


def test_unknown_feature_rejected_before_request():
    client, session = make()
    with pytest.raises(InvalidRequestError):
        client.transcript("jNQXAC9IVRw", features=["summary"])
    assert session.calls == []


@pytest.mark.parametrize(
    "status, code, cls",
    [
        (400, "INVALID_INPUT", InvalidRequestError),
        (401, "INVALID_API_KEY", AuthenticationError),
        (403, "PLAN_UPGRADE_REQUIRED", PlanRequiredError),
        (404, "TRANSCRIPT_NOT_FOUND", TranscriptNotFoundError),
        (422, "EXTRACTION_FAILED", ExtractionFailedError),
        (502, "FEATURES_FAILED", FeaturesFailedError),
        (503, "WORKER_UNAVAILABLE", ServiceUnavailableError),
        (500, "INTERNAL_ERROR", ServiceUnavailableError),
    ],
)
def test_errors_map_to_classes(status, code, cls):
    client, _ = make(FakeResponse(status, error_body(code, "nope")))
    with pytest.raises(cls) as info:
        client.transcript("jNQXAC9IVRw")
    assert info.value.code == code
    assert info.value.status == status
    assert KEY not in str(info.value)


def test_rate_limit_keeps_retry_after_and_partial_data():
    data = ok_body()["data"]
    client, _ = make(FakeResponse(429, error_body("QUOTA_EXCEEDED", data=data), {"Retry-After": "120"}))
    with pytest.raises(RateLimitError) as info:
        client.transcript("jNQXAC9IVRw", features=["hooks"])
    assert info.value.retry_after == 120
    assert info.value.data["transcript"]["text"]


def test_busy_409_is_a_rate_limit_error_with_transcript():
    client, _ = make(FakeResponse(409, error_body("BUSY", data=ok_body()["data"])))
    with pytest.raises(RateLimitError) as info:
        client.transcript("jNQXAC9IVRw", features=["hooks"])
    assert info.value.code == "BUSY" and info.value.data["transcript"]["text"]


def test_proxy_502_page_is_an_outage_not_features_failed():
    client, _ = make(FakeResponse(502, None))
    with pytest.raises(ServiceUnavailableError):
        client.transcript("jNQXAC9IVRw")


def test_context_manager_closes_session():
    closed = []
    session = FakeSession()
    session.close = lambda: closed.append(True)
    with TubeTranscript(KEY, session=session):
        pass
    assert closed == [True]


def test_features_failed_keeps_details():
    client, _ = make(FakeResponse(502, error_body("FEATURES_FAILED", data=ok_body()["data"], details={"hooks": "AI_TIMEOUT"})))
    with pytest.raises(FeaturesFailedError) as info:
        client.transcript("jNQXAC9IVRw", features=["hooks"])
    assert info.value.details == {"hooks": "AI_TIMEOUT"}


def test_non_json_error_body():
    client, _ = make(FakeResponse(502, None))
    with pytest.raises(ServiceUnavailableError) as info:
        client.transcript("jNQXAC9IVRw")
    assert info.value.code == "HTTP_502"


def test_network_error_wrapped():
    client, _ = make(requests.ConnectionError("boom"))
    with pytest.raises(ServiceUnavailableError) as info:
        client.transcript("jNQXAC9IVRw")
    assert info.value.code == "NETWORK_ERROR"


def test_202_is_retried_until_success():
    pending = FakeResponse(202, error_body("EXTRACTION_IN_PROGRESS", data={"job_id": "j1", "status": "processing"}),
                           {"Retry-After": "5"})
    sleeps = []
    client, session = make(pending, pending, FakeResponse(200, ok_body(source="audio")), sleeps=sleeps)
    result = client.transcript("jNQXAC9IVRw")
    assert result.source == "audio"
    assert sleeps == [5.0, 5.0]
    assert len(session.calls) == 3
    assert session.calls[0]["json"] == session.calls[2]["json"]


def test_202_gives_up_after_wait():
    pending = FakeResponse(202, error_body("EXTRACTION_IN_PROGRESS", data={"job_id": "j1", "status": "queued"}))
    client, session = make(pending)
    with pytest.raises(ExtractionInProgressError) as info:
        client.transcript("jNQXAC9IVRw", wait=0)
    assert info.value.job_id == "j1"
    assert len(session.calls) == 1


def test_srt_format():
    client, _ = make(FakeResponse(200, ok_body()))
    srt = to_srt(client.transcript("jNQXAC9IVRw").chunks)
    assert srt.startswith("1\n00:00:01,200 --> 00:00:03,360\nAll right, so here we are\n")
    assert "2\n00:00:03,360 --> 00:00:06,560\n" in srt


def test_cli_without_key_fails_cleanly(capsys):
    assert main(["jNQXAC9IVRw"]) == 1
    assert "MISSING_API_KEY" in capsys.readouterr().err
