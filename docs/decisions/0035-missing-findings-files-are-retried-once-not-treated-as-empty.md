# 0035 Missing findings files are retried once, not treated as empty
Status: accepted 2026-11-21
Decision: The review skills re-dispatch each reviewer that ingest reports missing, once, then ingest again. The reviewer prompt makes the findings write the last tool call. ingest still reports a missing file as missing, never as empty.
Why: A quiet reviewer sometimes skipped the file, so a PR was reviewed without a panel member unnoticed. A crashed reviewer and a quiet one must stay distinguishable.
Rejected: Treating a missing file as empty hides crashes. Retrying more than once: a second failure is likelier a real fault.
