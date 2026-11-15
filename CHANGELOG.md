# Changelog

## 0.1.0 (2026-11-15)

- `YouTubeTranscriptApi`: drop-in for youtube-transcript-api 1.2+. `fetch()` falls back to the TubeTranscript API on `RequestBlocked`, `IpBlocked`, `PoTokenRequired`, `FailedToCreateConsentCookie`, `YouTubeDataUnparsable` and `YouTubeRequestFailed`; optionally on `TranscriptsDisabled`.
- `TubeTranscript` client for `POST /api/v1/extract`: typed errors, waiting while audio transcription runs, retries with backoff for transient failures, no redirects, API key format check before any request.
- `tubetranscript` command: text, JSON or SRT output, `-v` for debug logs.
- Debug logging on the `tubetranscript` logger; the key and transcript text are never logged.
