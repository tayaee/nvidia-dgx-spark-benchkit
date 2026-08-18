"""FakeOpenAIEndpoint — in-process OpenAI-compatible endpoint used by tests.

Mirrors the minimal contract the runner needs:
- POST /v1/chat/completions  -> returns a deterministic assistant message
- GET  /v1/models            -> lists available models

It deliberately avoids HTTP plumbing — tests pass a callable into
AttemptRunner directly. End-to-end network tests should use a tiny
HTTP wrapper (out of scope here).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeOpenAIEndpoint:
    model_name: str = "fake-model"
    responses: list[dict] = field(default_factory=list)
    fail_first_n: int = 0
    calls: list[dict] = field(default_factory=list)

    def chat(self, messages, model=None, **kw):
        self.calls.append({"messages": messages, "model": model, **kw})
        if self.fail_first_n > 0:
            self.fail_first_n -= 1
            raise RuntimeError("fake endpoint: simulated network failure")
        if self.responses:
            content = self.responses.pop(0)
        else:
            content = "fake response"
        return {
            "id": "chatcmpl-fake",
            "model": model or self.model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def list_models(self):
        return {"data": [{"id": self.model_name, "object": "model"}]}

    def health_check(self) -> bool:
        return True