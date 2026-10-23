"""LLM client + prompt + structured-output validation / retry policy.

Policy (defended in APPROACH.md):
  * Never trust the model: strip fences, decode ONLY the outermost JSON value, validate with Pydantic.
  * Any structural violation => reject the WHOLE response and retry with the validation error
    fed back (max N attempts). We never repair items one by one or mix valid+invalid items.
  * >5 valid cases is harmless -> truncate to 5 and record a warning. <3 violates the contract -> retry.
  * Exhausted attempts => status "failed", raw attempts stored for debugging, API returns 502.
    Partial output is NEVER returned as if valid.
  * Cited source labels must exist in the prompt; numbers in a test case that appear in none of
    its cited sources are flagged (warning) as possible hallucination / mis-citation.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .analysis import numeric_facts

PROMPT_VERSION = "tc-v1"

SYSTEM_PROMPT = """You are a QA engineer writing test cases for a regulated medical device (home blood pressure monitor).
You are given numbered source excerpts from the device manual. Write between 3 and 5 test case ideas.

Rules:
- Use ONLY facts stated in the sources. Never invent thresholds, timings, codes or behaviours.
- Copy numeric values and units exactly as written in the sources.
- Each test case must be concrete enough that another engineer could execute it: explicit steps and a checkable expected result.
- Prefer boundary, negative and safety-relevant cases over restating the text.
- Every test case must cite the source labels it is derived from (e.g. "S1").
- Reply with ONE JSON object and nothing else (no prose, no markdown fences), exactly this shape:
{"test_cases":[{"title":"...","preconditions":["..."],"steps":["..."],"expected_result":"...","priority":"high|medium|low","source_labels":["S1"]}]}
"""


class TestCaseModel(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    title: str = Field(min_length=5, max_length=200)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1, max_length=20)
    expected_result: str = Field(min_length=5, max_length=600)
    priority: Literal["high", "medium", "low"]
    source_labels: list[str] = Field(min_length=1)

    @field_validator("priority", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.lower().strip() if isinstance(v, str) else v

    @field_validator("steps")
    @classmethod
    def _steps_nonempty(cls, v):
        if any(not s.strip() for s in v):
            raise ValueError("steps must be non-empty strings")
        return v


class GenerationOutput(BaseModel):
    test_cases: list[TestCaseModel] = Field(min_length=3)


class OutputInvalid(Exception):
    pass


class LLMTransportError(Exception):
    pass


class LLMClient(Protocol):
    name: str

    def complete(self, messages: list[dict]) -> str: ...


class OpenAICompatClient:
    """Works with Groq, OpenRouter and Gemini's OpenAI-compatible endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str, json_mode: bool = True, timeout: float = 60.0,
                 transport_retries: int = 2):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.json_mode, self.timeout, self.transport_retries = json_mode, timeout, transport_retries
        self.name = model

    def complete(self, messages: list[dict]) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0.2}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        last = None
        for i in range(self.transport_retries + 1):
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                               headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LLMTransportError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
            except (httpx.HTTPError, LLMTransportError, KeyError, IndexError, ValueError) as e:
                last = e
                time.sleep(0.5 * (3 ** i))
        raise LLMTransportError(f"LLM call failed after retries: {last}")


class MockLLM:
    """Deterministic offline stand-in (LLM_PROVIDER=mock). Output is built from the prompt text only."""
    name = "mock"

    def complete(self, messages: list[dict]) -> str:
        user = messages[1]["content"]
        cases = []
        for label, text in re.findall(r"\[(S\d+)\][^\n]*\n(.*?)(?=\n\[S\d+\]|\Z)", user, re.S):
            for sent in re.split(r"(?<=[.!?])\s+|\n", text):
                if re.search(r"\d", sent) and len(sent) > 25 and len(cases) < 5:
                    s = sent.strip(" |-")
                    cases.append({"title": f"Verify: {s[:70]}", "preconditions": ["Device powered on with fresh batteries"],
                                  "steps": ["Prepare the device as described in the source", f"Exercise the behaviour: {s}"],
                                  "expected_result": s, "priority": "high" if "mmHg" in s or "second" in s else "medium",
                                  "source_labels": [label]})
        labels = re.findall(r"\[(S\d+)\]", user)
        while len(cases) < 3 and labels:
            cases.append({"title": f"Smoke check for {labels[0]} requirement", "steps": ["Follow the section", "Observe device"],
                          "expected_result": "Device behaves as the section states", "priority": "low", "source_labels": [labels[0]]})
        return json.dumps({"test_cases": cases})


# ----------------------------------------------------------------------------- prompt + parsing
def build_messages(sources: list[dict]) -> list[dict]:
    parts = [f"[{s['label']}] Section {s['section_number']} - {s['title']}\n{s['body'] or '(no body text; see sub-sections)'}"
             for s in sources]
    user = "SOURCES\n\n" + "\n\n".join(parts) + "\n\nWrite 3 to 5 test cases now. JSON only."
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def extract_json(raw: str):
    s = (raw or "").strip()
    if not s:
        raise OutputInvalid("empty response")
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if fence:
        s = fence.group(1).strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        raise OutputInvalid("no JSON object found in response")
    try:  # only the OUTERMOST value: a truncated reply must fail, not yield an inner fragment
        obj, _ = json.JSONDecoder().raw_decode(s, min(starts))
    except json.JSONDecodeError as e:
        raise OutputInvalid(f"malformed or truncated JSON: {e.msg} at char {e.pos}")
    return obj


def _bare(c) -> set[str]:
    return {re.search(r"\d+(?:\.\d+)?", k).group(0) for k in c}


def parse_output(raw: str, valid_labels: set[str], sources_text: dict[str, str]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    obj = extract_json(raw)
    if isinstance(obj, list):
        obj, _ = {"test_cases": obj}, warnings.append("model returned a bare list; wrapped")
    if not isinstance(obj, dict) or "test_cases" not in obj:
        raise OutputInvalid('top-level JSON must be an object with a "test_cases" array')
    try:
        out = GenerationOutput.model_validate(obj)
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5]]
        raise OutputInvalid("schema violation: " + "; ".join(msgs))
    cases = out.test_cases
    if len(cases) > 5:
        warnings.append(f"model returned {len(cases)} test cases; truncated to 5")
        cases = cases[:5]
    result = []
    for i, tc in enumerate(cases, 1):
        labels = list(dict.fromkeys(tc.source_labels))
        unknown = [l for l in labels if l not in valid_labels]
        if unknown:
            raise OutputInvalid(f"test case {i} cites unknown source labels {unknown}; valid: {sorted(valid_labels)}")
        cited = " ".join(sources_text[l] for l in labels)
        text = " ".join([tc.title, *tc.preconditions, *tc.steps, tc.expected_result])
        unsupported = sorted(_bare(numeric_facts(text)) - _bare(numeric_facts(cited)))
        d = tc.model_dump()
        d["source_labels"] = labels
        d["unsupported_numbers"] = unsupported
        result.append(d)
    return result, warnings


@dataclass
class RunResult:
    ok: bool
    test_cases: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    failure_reason: str | None = None


def run_generation(client: LLMClient, sources: list[dict], max_attempts: int = 3) -> RunResult:
    valid = {s["label"] for s in sources}
    text = {s["label"]: f"{s['title']}\n{s['body']}" for s in sources}
    messages = build_messages(sources)
    attempts: list[dict] = []
    for n in range(1, max_attempts + 1):
        try:
            raw = client.complete(messages)
        except LLMTransportError as e:
            attempts.append({"n": n, "error": f"transport: {e}", "raw": None})
            return RunResult(False, attempts=attempts, failure_reason="llm_unavailable")
        try:
            cases, warns = parse_output(raw, valid, text)
            attempts.append({"n": n, "error": None, "raw": raw[:6000]})
            return RunResult(True, cases, warns, attempts)
        except OutputInvalid as e:
            attempts.append({"n": n, "error": str(e), "raw": raw[:6000]})
            messages = messages[:2] + [
                {"role": "assistant", "content": (raw or "")[:4000]},
                {"role": "user", "content": f"Your previous reply was rejected: {e}. Reply again with ONLY the JSON object "
                                            f"described in the instructions: 3-5 test cases, valid source_labels."},
            ]
    return RunResult(False, attempts=attempts, failure_reason="invalid_output_after_retries")


def make_llm(settings) -> LLMClient | None:
    if settings.llm_provider == "mock":
        return MockLLM()
    if not settings.llm_api_key:
        return None
    return OpenAICompatClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                              settings.llm_json_mode, settings.llm_timeout_s)
