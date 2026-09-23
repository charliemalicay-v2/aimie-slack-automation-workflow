import httpx
import pytest

from app.llm import TOOL_NAME, LLMAuthError, OllamaClassifier
from app.retry import RetriesExhausted


def tool_response(inp: dict) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": "",
                          "tool_calls": [{"function": {"name": TOOL_NAME, "arguments": inp}}]}})


GOOD = {"label": "urgent", "confidence": 0.93, "reason": "Checkout is down", "suggested_action": "Page on-call"}


def make(responses):
    """Classifier whose HTTP layer replays `responses` in order and records requests."""
    seen, it = [], iter(responses)

    def handler(request):
        seen.append(request)
        return next(it)

    sleeps = []
    clf = OllamaClassifier("http://localhost:11434", "llama-test",
                           httpx.Client(transport=httpx.MockTransport(handler)),
                           http_max_attempts=3, validation_attempts=2, sleep=sleeps.append)
    return clf, seen, sleeps


def test_valid_structured_output():
    clf, seen, _ = make([tool_response(GOOD)])
    out = clf.classify("checkout is down")
    assert out.result.label == "urgent" and not out.is_fallback and out.attempts == 1
    body = seen[0].read().decode()
    assert '"tools"' in body and TOOL_NAME in body


@pytest.mark.parametrize("bad", [
    {**GOOD, "label": "critical"},          # not an allowed label
    {**GOOD, "confidence": 7},              # out of range
    {"label": "urgent"},                    # missing fields
    {**GOOD, "extra": "field"},             # unexpected field
])
def test_malformed_then_repaired(bad):
    clf, seen, _ = make([tool_response(bad), tool_response(GOOD)])
    out = clf.classify("x")
    assert out.result.label == "urgent" and out.attempts == 2 and not out.is_fallback
    assert "previous answer was invalid" in seen[1].read().decode()


def test_text_instead_of_tool_call_falls_back_after_repair_fails():
    text_only = httpx.Response(200, json={"message": {"role": "assistant", "content": "I think it's urgent"}})
    clf, _, _ = make([text_only, text_only])
    out = clf.classify("x")
    assert out.is_fallback and out.result.label == "action" and out.result.confidence == 0.0
    assert "no tool call in response" in out.error


def test_non_json_body_is_treated_as_malformed():
    clf, _, _ = make([httpx.Response(200, text="<html>oops</html>"), tool_response(GOOD)])
    assert clf.classify("x").attempts == 2


def test_rate_limit_honours_retry_after():
    clf, seen, sleeps = make([httpx.Response(429, headers={"retry-after": "7"}), tool_response(GOOD)])
    assert clf.classify("x").result.label == "urgent"
    assert sleeps == [7.0] and len(seen) == 2


def test_overloaded_503_retries_then_gives_up():
    clf, seen, sleeps = make([httpx.Response(503)] * 3)
    with pytest.raises(RetriesExhausted):
        clf.classify("x")
    assert len(seen) == 3 and len(sleeps) == 2


def test_model_not_found_is_not_retried():
    clf, seen, _ = make([httpx.Response(404, json={"error": "model 'llama-test' not found"})])
    with pytest.raises(LLMAuthError):
        clf.classify("x")
    assert len(seen) == 1


def test_timeout_is_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return tool_response(GOOD)

    clf = OllamaClassifier("http://localhost:11434", "m",
                           httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert clf.classify("x").result.label == "urgent"
