import json

from app.llm import LLMTransportError, MockLLM, build_messages, extract_json, run_generation
from tests.helpers import SOURCES, ScriptedLLM, payload, tc

GOOD = payload(tc(), tc("Second case here"), tc("Third case here"))


def test_valid_response_first_try():
    r = run_generation(ScriptedLLM([GOOD]), SOURCES)
    assert r.ok and len(r.test_cases) == 3 and len(r.attempts) == 1


def test_fenced_and_prose_wrapped_json_is_accepted():
    r = run_generation(ScriptedLLM(["Sure! Here you go:\n```json\n" + GOOD + "\n```\nHope that helps"]), SOURCES)
    assert r.ok


def test_malformed_then_valid_retries_with_error_fed_back():
    llm = ScriptedLLM(['{"test_cases": [{"title": "cut off', GOOD])
    r = run_generation(llm, SOURCES)
    assert r.ok and len(r.attempts) == 2 and "malformed or truncated JSON" in r.attempts[0]["error"]
    retry_msgs = llm.calls[1]
    assert retry_msgs[-1]["role"] == "user" and "rejected" in retry_msgs[-1]["content"]


def test_truncated_reply_is_not_salvaged_as_inner_fragment():
    # a naive "find first parseable {...}" would return the first complete test case object
    truncated = '{"test_cases":[' + json.dumps(tc()) + ',{"title":"unfinished'
    r = run_generation(ScriptedLLM([truncated] * 3), SOURCES)
    assert not r.ok and r.test_cases == [] and r.failure_reason == "invalid_output_after_retries"


def test_too_few_cases_is_rejected_then_retried():
    r = run_generation(ScriptedLLM([payload(tc(), tc("Another case here")), GOOD]), SOURCES)
    assert r.ok and len(r.attempts) == 2 and "schema violation" in r.attempts[0]["error"]


def test_too_many_cases_is_truncated_with_warning_not_rejected():
    six = payload(*[tc(f"Case number {i}") for i in range(6)])
    r = run_generation(ScriptedLLM([six]), SOURCES)
    assert r.ok and len(r.test_cases) == 5 and any("truncated to 5" in w for w in r.warnings)


def test_unknown_source_label_is_rejected():
    bad = payload(tc(), tc("Another case here"), tc("Third case here", labels=("S9",)))
    r = run_generation(ScriptedLLM([bad, GOOD]), SOURCES)
    assert r.ok and "unknown source labels" in r.attempts[0]["error"]


def test_missing_required_field_and_bad_priority_rejected():
    bad = json.dumps({"test_cases": [{"title": "No steps at all", "expected_result": "something", "priority": "urgent",
                                      "source_labels": ["S1"]}] * 3})
    r = run_generation(ScriptedLLM([bad, bad, bad]), SOURCES, max_attempts=3)
    assert not r.ok and len(r.attempts) == 3


def test_all_attempts_fail_returns_failure_with_raw_kept_and_no_cases():
    r = run_generation(ScriptedLLM(["I cannot help with that."] * 3), SOURCES)
    assert not r.ok and r.test_cases == [] and all(a["raw"] for a in r.attempts)


def test_transport_failure_is_reported_as_unavailable_not_invalid_output():
    r = run_generation(ScriptedLLM([LLMTransportError("HTTP 503")]), SOURCES)
    assert not r.ok and r.failure_reason == "llm_unavailable"


def test_unsupported_numbers_are_flagged_not_dropped():
    hallucinated = payload(tc(expected="Cuff vents within 5 seconds of pressure above 299 mmHg"), tc("Second case here"), tc("Third case here"))
    r = run_generation(ScriptedLLM([hallucinated]), SOURCES)
    assert r.ok and r.test_cases[0]["unsupported_numbers"] == ["5"]
    assert r.test_cases[1]["unsupported_numbers"] == []


def test_bare_list_is_wrapped_with_warning():
    r = run_generation(ScriptedLLM([json.dumps([tc(), tc("Another case here"), tc("Third case here")])]), SOURCES)
    assert r.ok and any("bare list" in w for w in r.warnings)


def test_extract_json_rejects_prose_only():
    import pytest
    from app.llm import OutputInvalid
    with pytest.raises(OutputInvalid):
        extract_json("no json here")


def test_mock_llm_output_passes_the_same_validation():
    r = run_generation(MockLLM(), SOURCES)
    assert r.ok and 3 <= len(r.test_cases) <= 5
    assert "[S1]" in build_messages(SOURCES)[1]["content"]
