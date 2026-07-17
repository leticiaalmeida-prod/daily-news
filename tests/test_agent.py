from __future__ import annotations

from bot.agent import FALLBACK, respond


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _ToolUseBlock:
    def __init__(self, name: str, input: dict, id: str = "tool_1") -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input
        self.id = id


class _Response:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _ScriptedMessages:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeLLM:
    def __init__(self, responses: list) -> None:
        self.messages = _ScriptedMessages(responses)


class _FakeTools:
    def __init__(self) -> None:
        self.called_with: list[tuple] = []

    def tools_for_llm(self) -> list[dict]:
        return [{"name": "search", "description": "d", "input_schema": {"type": "object"}}]

    def call(self, name: str, args) -> str:
        self.called_with.append((name, args))
        return '{"data": "fake result"}'


def test_respond_returns_text_immediately_on_end_turn() -> None:
    llm = _FakeLLM([_Response([_TextBlock("hello there")], "end_turn")])
    reply = respond(
        "hi", llm=llm, tools=_FakeTools(), model="fake", system="sys"
    )
    assert reply == "hello there"


def test_respond_executes_tool_then_returns_final_text() -> None:
    tools = _FakeTools()
    llm = _FakeLLM(
        [
            _Response([_ToolUseBlock("search", {"q": "x"})], "tool_use"),
            _Response([_TextBlock("final answer")], "end_turn"),
        ]
    )
    reply = respond("query", llm=llm, tools=tools, model="fake", system="sys")
    assert reply == "final answer"
    assert tools.called_with == [("search", {"q": "x"})]


def test_respond_falls_back_when_max_iters_exhausted() -> None:
    # Every turn wants another tool call — never reaches end_turn.
    responses = [_Response([_ToolUseBlock("search", {})], "tool_use") for _ in range(4)]
    llm = _FakeLLM(responses)
    reply = respond(
        "query", llm=llm, tools=_FakeTools(), model="fake", system="sys", max_iters=4
    )
    assert reply == FALLBACK
    assert len(llm.messages.calls) == 4  # bounded, never looped past max_iters


def test_respond_falls_back_on_empty_text_response() -> None:
    llm = _FakeLLM([_Response([], "end_turn")])
    reply = respond("hi", llm=llm, tools=_FakeTools(), model="fake", system="sys")
    assert reply == FALLBACK
