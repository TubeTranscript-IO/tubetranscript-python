"""Command line: ``tubetranscript <video URL or ID> [--format text|json|srt] [--features hooks,mindmap]``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from typing import List, Optional, Sequence

from ._client import FEATURES, Chunk, TubeTranscript, iter_features
from ._errors import TubeTranscriptError
from ._version import __version__


def _srt_time(seconds: float) -> str:
    millis = max(round(seconds * 1000), 0)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(chunks: Sequence[Chunk]) -> str:
    blocks: List[str] = []
    for index, chunk in enumerate(chunks, start=1):
        end = chunk.start + chunk.duration
        blocks.append(f"{index}\n{_srt_time(chunk.start)} --> {_srt_time(end)}\n{chunk.text.strip()}\n")
    return "\n".join(blocks)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tubetranscript",
        description="Get a video transcript from the TubeTranscript API (needs TUBETRANSCRIPT_API_KEY).",
    )
    parser.add_argument("video", help="YouTube video URL or ID, or a TikTok, Instagram or Facebook video URL")
    parser.add_argument("--format", choices=("text", "json", "srt"), default="text")
    parser.add_argument("--features", action="append", default=[],
                        help=f"AI extras, comma-separated: {', '.join(FEATURES)} (implies --format json)")
    parser.add_argument("--wait", type=float, default=600.0,
                        help="Seconds to keep waiting for audio transcription (default 600)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log retries and waits to stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="tubetranscript: %(message)s")
        # urllib3 debug lines would print request URLs and headers' presence; keep them out.
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Windows consoles default to a legacy code page: never crash on characters it cannot show.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    features = iter_features(args.features)
    try:
        result = TubeTranscript().transcript(args.video, features=features, wait=args.wait)
    except TubeTranscriptError as error:
        print(f"tubetranscript: {error}", file=sys.stderr)
        return 1

    if args.format == "json" or features:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    elif args.format == "srt":
        print(to_srt(result.chunks))
    else:
        print(result.text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
