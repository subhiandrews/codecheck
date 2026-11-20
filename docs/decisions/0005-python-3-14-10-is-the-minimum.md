# 0005 Python 3.14.10 is the minimum
Status: accepted 2026-11-12
Decision: The minimum supported Python is 3.14.10.
Why: Releases up to 3.14.10 fixed security bugs in `tempfile` cleanup and `tarfile` link handling, which matter to a tool that writes files and extracts archives.
Rejected: Setting the floor at 3.14.8, itself a security release.
