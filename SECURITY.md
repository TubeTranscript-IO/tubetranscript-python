# Security policy

This repository holds the `tubetranscript` Python package. The TubeTranscript API it calls is a hosted service and is not in this repository.

## Supported versions

Security fixes go into the latest release on [PyPI](https://pypi.org/project/tubetranscript/). Upgrade with `pip install -U tubetranscript`.

## Reporting a vulnerability

Email **support@tubetranscript.io** with "Security" in the subject, a description of the problem and the steps to reproduce it. Please do not open a public issue for a vulnerability, whether it is in this package (for example a way the API key could end up in logs, exceptions or a request to another host) or in the hosted API. We will reply by email.

A bug that does not expose keys, data or accounts can go in a normal issue.

## Never post your API key

Do not paste a real API key (`tt_live_...`) into an issue, a discussion, a pull request, a traceback or a screenshot. Replace it with the placeholder `tt_live_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX` first.

If a key was exposed, revoke it in your TubeTranscript dashboard and create a new one.
