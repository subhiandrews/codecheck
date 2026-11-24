from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest
from judge import AnthropicJudge
from pydantic import BaseModel


class Verdict(BaseModel):
    score: int
    reason: str


def _reply(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def _block(kind: str, text: str = ""):
    return SimpleNamespace(type=kind, text=text) if kind == "text" else SimpleNamespace(type=kind)


def test_parse_skips_a_leading_thinking_block():
    reply = _reply(_block("thinking"), _block("text", 'Here: {"score": 7, "reason": "close"}'))
    assert AnthropicJudge._parse(reply, Verdict) == Verdict(score=7, reason="close")


def test_parse_without_schema_returns_the_text():
    assert AnthropicJudge._parse(_reply(_block("text", "hello")), None) == "hello"


def test_parse_without_a_text_block_names_the_stop_reason():
    with pytest.raises(ValueError, match="max_tokens"):
        AnthropicJudge._parse(_reply(_block("thinking"), stop_reason="max_tokens"), None)


def test_the_telemetry_opt_out_is_set_unless_the_environment_says_otherwise(monkeypatch):
    import _env

    monkeypatch.delenv("DEEPEVAL_TELEMETRY_OPT_OUT", raising=False)
    importlib.reload(_env)
    assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "YES"
    monkeypatch.setenv("DEEPEVAL_TELEMETRY_OPT_OUT", "NO")
    importlib.reload(_env)
    assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "NO"
