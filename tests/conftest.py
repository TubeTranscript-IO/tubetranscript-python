from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
import requests


class FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.reason = "Reason"

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Records POSTs and replays queued responses (or raises queued exceptions)."""

    def __init__(self, *responses: Any) -> None:
        self.responses: List[Any] = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        # Apply an AuthBase like requests does, so tests can read the resulting headers.
        auth = kwargs.get("auth")
        if auth is not None:
            prepared = requests.PreparedRequest()
            prepared.headers = requests.structures.CaseInsensitiveDict(kwargs.get("headers") or {})
            auth(prepared)
            kwargs = {**kwargs, "headers": dict(prepared.headers)}
        self.calls.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok_body(**overrides: Any) -> Dict[str, Any]:
    data = {
        "video_id": "jNQXAC9IVRw",
        "platform": "youtube",
        "job_id": None,
        "source": "captions",
        "language": "en",
        "video_title": None,
        "channel_name": None,
        "transcript": {
            "text": "All right, so here we are\n\nin front of the elephants",
            "chunks": [
                {"start": 1.2, "duration": 2.16, "text": "All right, so here we are"},
                {"start": 3.36, "duration": 3.2, "text": "in front of the elephants"},
            ],
        },
        "features": {},
    }
    data.update(overrides)
    return {"success": True, "data": data, "error": None}


def error_body(code: str, message: str = "msg", data: Any = None, details: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"success": False, "data": data, "error": error}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TUBETRANSCRIPT_API_KEY", raising=False)
    monkeypatch.delenv("TUBETRANSCRIPT_BASE_URL", raising=False)


# Well-formed but made-up keys (the client checks the tt_live_ + 32 characters format).
KEY = "tt_live_" + "A" * 32
ENV_KEY = "tt_live_" + "B" * 32
