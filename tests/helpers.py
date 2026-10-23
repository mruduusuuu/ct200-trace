import json


class ScriptedLLM:
    """Returns queued responses in order (str, or an Exception instance to raise). Records calls."""
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def tc(title="Verify the stated behaviour", labels=("S1",), expected="Device behaves as stated in the source", priority="high"):
    return {"title": title, "preconditions": [], "steps": ["Do the thing"], "expected_result": expected,
            "priority": priority, "source_labels": list(labels)}


def payload(*cases):
    return json.dumps({"test_cases": list(cases)})


SOURCES = [{"label": "S1", "section_number": "4.1", "title": "Overpressure Protection",
            "body": "Vents the cuff within 2 seconds if pressure exceeds 299 mmHg."}]
