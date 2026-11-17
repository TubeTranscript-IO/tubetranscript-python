# tubetranscript

YouTube transcripts that keep working on cloud servers.

If you use [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api) on AWS, GCP, Azure, Vercel or a VPS, you have probably seen this:

```
youtube_transcript_api._errors.RequestBlocked: Could not retrieve a transcript for the video ...
YouTube is blocking requests from your IP.
```

`tubetranscript` is a drop-in replacement. Change one import. The direct request to YouTube still runs first, at no cost. When YouTube blocks it, the call falls back to the [TubeTranscript API](https://tubetranscript.io/youtube-transcript-api) and you get back the same `FetchedTranscript` object.

```bash
pip install tubetranscript
export TUBETRANSCRIPT_API_KEY=tt_live_...   # create one at https://tubetranscript.io/dashboard/api
```

```python
# from youtube_transcript_api import YouTubeTranscriptApi
from tubetranscript import YouTubeTranscriptApi

fetched = YouTubeTranscriptApi().fetch("jNQXAC9IVRw")
for snippet in fetched:
    print(snippet.start, snippet.text)
```

Formatters from `youtube_transcript_api.formatters` work on the result unchanged. Requires Python 3.9+ and youtube-transcript-api 1.2+.

## Why YouTube blocks you

YouTube rate-limits and blocks requests from IP ranges that belong to cloud providers. Your code works on your laptop and fails in production. You can fix that yourself with rotating residential proxies (`youtube-transcript-api` supports them through `proxy_config`), or let an API do it for you. This package is the second option, and it only kicks in when the first request fails.

## Drop-in options

```python
YouTubeTranscriptApi(
    api_key=None,                    # default: TUBETRANSCRIPT_API_KEY
    fallback_on_no_captions=False,   # also send videos with captions disabled to the API
    try_direct_first=True,           # False: skip the direct request (servers YouTube always blocks)
    wait=600,                        # seconds to keep waiting while the API transcribes audio
    proxy_config=None,               # passed to youtube-transcript-api as before
    http_client=None,                # passed to youtube-transcript-api as before
)
```

Without an API key the class behaves exactly like the original and raises the original errors, with a one-time hint. The one exception: `try_direct_first=False` without a key raises `ConfigurationError`.

**When it falls back.** On errors where YouTube refused or confused your machine: `RequestBlocked` (including `IpBlocked`), `PoTokenRequired`, `FailedToCreateConsentCookie`, `YouTubeDataUnparsable` and `YouTubeRequestFailed`. Errors about the video itself (`VideoUnavailable`, `AgeRestricted`, `NoTranscriptFound`, `InvalidVideoId`...) are raised unchanged. When the API call fails too, its error is raised with the original YouTube error as `__cause__`.

**Videos without captions.** With `fallback_on_no_captions=True`, a `TranscriptsDisabled` video is also sent to the API. Note that once a call reaches the API for any reason, the API transcribes the audio whenever the video has no captions. That uses your plan's audio allowance and can take minutes. The client keeps resending while it runs, for about `wait` seconds plus up to 5 minutes for the request in flight; each resend counts as one API request.

**Snippet length differs on the fallback path.** YouTube returns one snippet per on-screen caption line (a few seconds each). The API returns passages of about 15 to 30 seconds, cut at sentence ends, which suits LLM prompts. Code that reads `snippet.text`, `snippet.start` and `snippet.duration` keeps working; code that expects caption-line timing should not rely on it after a fallback.

**What the fallback does not do.** Only `fetch()` falls back. `list()` and `translate()` still talk to YouTube directly. On the fallback path, `languages` and `preserve_formatting` are not applied: you get the video's default transcript language, reported in `fetched.language_code` (empty when the API does not know it). `is_generated` is `True` when the API transcribed the audio, and `False` for captions, because the API does not say whether they were uploaded or auto-generated.

## Using the API directly

```python
from tubetranscript import TubeTranscript

with TubeTranscript() as client:  # reads TUBETRANSCRIPT_API_KEY
    result = client.transcript("https://www.youtube.com/watch?v=jNQXAC9IVRw", features=["hooks"])

print(result.source)      # "captions", "audio", "media" or "existing_job"
print(result.text)
print(result.chunks[0])   # Chunk(text=..., start=0.0, duration=4.2)
print(result.features["hooks"])
print(result.rate_limit_remaining)
```

`result.chunks` are passages of about 15 to 30 seconds. `transcript()` accepts a YouTube video ID or URL (`watch`, `youtu.be`, `shorts`, `embed`, `live`) and TikTok, Instagram and Facebook video links. For those, `result.video_id` is an internal key, not the platform's ID. `features` can include `hooks`, `mindmap` and `action_items`.

Call `client.close()` when done, or use the client as a context manager. The client uses a `requests.Session`; sharing one between threads works in practice, and one client per thread is the strict-safe choice.

### Retries

Transient failures are retried twice by default, with exponential backoff and jitter:

- Always, because the API cannot have counted the request: no connection could be made (DNS, refused, connect timeout) and `RATE_LIMIT_UNAVAILABLE`.
- Only for calls without `features`: a connection dropped after the request was sent, and a 502/503 page from the gateway in front of the API. These may already have counted one request; with `features` they could also have charged AI runs, so they are not resent.

Quota refusals (429), `WORKER_UNAVAILABLE` (maintenance, usually minutes), other 5xx errors and read timeouts are not retried. Set `TubeTranscript(max_retries=0)` to turn retries off.

### Errors

Every error is a `TubeTranscriptError` with `.code`, `.status`, `.message`, `.details` and, when the API sent one, `.data`.

| Exception | When |
|---|---|
| `ConfigurationError` | No API key, a key not in the `tt_live_` format, a non-https base URL, or a base URL that redirects |
| `AuthenticationError` | Unknown or revoked key (401) |
| `PlanRequiredError` | The key's team is not on Pro or Enterprise (403) |
| `InvalidRequestError` | The URL or the features were rejected (400) |
| `RateLimitError` | Allowance used up (429), or another AI run of your team is in progress (409 `BUSY`); see `.retry_after` |
| `TranscriptNotFoundError` | Video unavailable (404) |
| `ExtractionFailedError` | Private, removed, no speech (422) |
| `ExtractionInProgressError` | Audio transcription still running after `wait` seconds. Call again with the same URL; the job resumes. See `.job_id` |
| `FeaturesFailedError` | Some AI features failed (502); `.data` has the transcript and the features that worked |
| `ServiceUnavailableError` | 5xx, network failure or read timeout, after retries |
| `TubeTranscriptError` (base) | Anything else, for example `UNEXPECTED_RESPONSE` (a non-API page, usually a wrong `base_url`) |

When you asked for `features` and the API refuses or fails them (409, 429, 502 or 503 about AI), the transcript is still in `error.data["transcript"]`.

### Logging

The package logs fallbacks, retries and waits at `DEBUG` on the `tubetranscript` logger. It never logs the API key or transcript text.

```python
import logging
logging.basicConfig()
logging.getLogger("tubetranscript").setLevel(logging.DEBUG)
```

## Command line

```bash
tubetranscript https://youtu.be/jNQXAC9IVRw
tubetranscript jNQXAC9IVRw --format srt > video.srt
tubetranscript jNQXAC9IVRw --features hooks,mindmap   # JSON
tubetranscript jNQXAC9IVRw -v                          # log retries and waits
```

## Pricing

The direct path is free. The fallback needs an API key from the TubeTranscript Pro or Enterprise plan; see [pricing](https://tubetranscript.io/pricing) for current limits.

## Security and privacy

- The key is read from the argument or `TUBETRANSCRIPT_API_KEY`, sent only in the `Authorization` header (a `~/.netrc` entry cannot replace it), and never logged or included in error messages or `repr()`. Network exceptions chained as `__cause__` have their request stripped, so error trackers do not capture the header.
- Requests go to `https://tubetranscript.io` only when the client falls back. `TUBETRANSCRIPT_BASE_URL` can point elsewhere: https only (http for localhost), no credentials in the URL. Redirects are never followed, so the key cannot be forwarded to another address.
- The direct path is youtube-transcript-api's own code; nothing from it is sent to TubeTranscript.

Report a security issue privately to support@tubetranscript.io (subject: "Security"), not in a public issue.

## Contributing

Issues and pull requests are welcome at https://github.com/TubeTranscript-IO/tubetranscript-python. The package is developed alongside the TubeTranscript service and synced to this repository, so an accepted pull request is applied upstream and arrives back here in the next sync, credited to you.

## Disclosure

This package is made by the team behind TubeTranscript. It is not affiliated with `youtube-transcript-api` or its author, whose library does the direct fetching here and is a dependency of this package.

MIT License.
