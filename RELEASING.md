# Releasing `tubetranscript`

Releases are published to PyPI by `.github/workflows/ci.yml` through **Trusted Publishing**: no PyPI token is stored in this repository.

## Setup (once, maintainers)

- PyPI trusted publisher for project `tubetranscript`: owner `mrkenzi`, repository `tubetranscript-python`, workflow `ci.yml`, environment `pypi`.
- GitHub environment `pypi`: a maintainer is a required reviewer, and only `v*` tags may deploy to it.

## Each release

1. Bump `src/tubetranscript/_version.py` and add a CHANGELOG entry.
2. Check locally:
   ```bash
   python -m pip install -e ".[dev]"
   python -m ruff check . && python -m mypy && python -m pytest -q
   ```
3. Check against the live API with a Pro key (each call counts as one request):
   ```bash
   export TUBETRANSCRIPT_API_KEY=tt_live_...
   python -c "from tubetranscript import YouTubeTranscriptApi as A; f = A(try_direct_first=False).fetch('jNQXAC9IVRw'); print(len(f), f[0])"
   tubetranscript jNQXAC9IVRw --format srt -v | head
   ```
4. Tag and push:
   ```bash
   git tag -a v0.1.0 -m "tubetranscript 0.1.0"
   git push origin v0.1.0
   ```
   CI runs the tests on Python 3.9–3.14 against youtube-transcript-api 1.2.0 and the latest version, lints, builds, checks that the tag matches the built version, waits for a maintainer's approval in the `pypi` environment, and publishes.
5. Check https://pypi.org/project/tubetranscript/ and `pip install tubetranscript==<version>` in a clean environment.

A version on PyPI can never be re-uploaded. If something is wrong, yank it on PyPI and release the next patch version.

## Compatibility rules

- The client follows the public contract of `POST /api/v1/extract` (https://tubetranscript.io/docs/api).
- The API key format check (`tt_live_` + 32 characters) mirrors the API. If the format ever changes, the SDK must be released first, because older versions reject keys in a new format.
