"""Opt out of DeepEval telemetry. Import this before anything imports deepeval.

DeepEval reports anonymous usage (counts, metric names, the judge model name) to PostHog unless this is set.
It does not carry prompts or findings as far as the code shows, but the evals run on private review data and
only need to talk to the judge, so they stay off the network otherwise. An explicit value in the environment wins.
"""

import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
