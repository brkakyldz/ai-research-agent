"""The one test that drives the real `langchain-openai` structured-output path.

Every other model test replaces the client with a fake whose `ainvoke` returns
`{"parsed": ..., "raw": ...}` - the shape `nodes/summarize.py`, `nodes/rank.py`
and `evals/judge.py` read. A fake that returns the shape the code expects can
never disagree with the library about it, so a release that changes the contract
passes the whole suite and fails on the first real call: after the money has
left, on every branch at once, closing the run with zero summaries.

This drives the real `ChatOpenAI`, the real `with_structured_output` and the
real pydantic parsing, and stubs only the transport. `httpx.MockTransport` and
not `respx`: the OpenAI SDK no longer routes through the transport hooks respx
patches, so a respx-mocked test of this path reaches the network and passes or
fails on whether a key happens to be present. A transport handed to the client
cannot reach anything.

What the recorded responses below pin down is the shape *as installed*, which is
not the shape the code was written against:

- The request asks for `response_format: json_schema`. There are no tools in it,
  so a recorded tool-call answer is not a valid recording of this path.
- A well-formed answer arrives as `parsed`, with `parsing_error` None.
- A **schema violation raises** out of `ainvoke`. The SDK parses eagerly, so
  pydantic's `ValidationError` propagates rather than arriving as
  `parsing_error`. Both nodes catch it; the point is that they must.
- An empty answer arrives as `parsed=None` with a `parsing_error` that says so.
- An answer in the *previous* mechanism's shape - a tool call - arrives as
  `parsed=None` and `parsing_error=None`, a failure with nothing to log. Both
  nodes branch on `parsed`, which is the only reading that survives it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ainews.config import Settings
from ainews.pipeline.llm import make_llm, usage_from_message
from ainews.pipeline.state import ArticleSummary

# One article's worth of answer, as the model returns it under a json_schema
# request: a JSON string in the message content.
ANSWER = json.dumps(
    {
        "title_local": "Yeni bir model duyuruldu",
        "summary": "Bir sirket yeni bir model yayinladi. Model ucuz. Yeni olan fiyat.",
        "why_it_matters": "Fiyat rekabeti degisiyor.",
        "tags": ["openai", "models"],
        "importance": 4,
    }
)

# The token counts a real press measures, so the numbers under test are the ones
# `pricing.SUMMARIZE_TOKENS_PER_ARTICLE` was derived from.
TOKENS_IN, TOKENS_OUT = 1438, 513


def _completion(content: str | None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-recorded",
        "object": "chat.completion",
        "created": 1_757_000_000,
        "model": "gpt-5.6-luna",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": TOKENS_IN,
            "completion_tokens": TOKENS_OUT,
            "total_tokens": TOKENS_IN + TOKENS_OUT,
        },
    }


def _client(content: str | None, requests: list[dict[str, Any]] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(content))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _structured(settings: Settings, content: str | None, requests: Any = None) -> Any:
    return make_llm(
        "gpt-5.6-luna", settings, http_async_client=_client(content, requests)
    ).with_structured_output(ArticleSummary, include_raw=True)


@pytest.fixture
def keyed_settings(settings: Settings) -> Settings:
    """`make_llm` refuses to build a client without a key, and this is about the
    response rather than about that guard."""
    return settings.model_copy(update={"openai_api_key": "sk-test-not-a-real-key"})


async def test_a_good_answer_arrives_as_parsed_with_the_three_contract_keys(
    keyed_settings: Settings,
) -> None:
    response = await _structured(keyed_settings, ANSWER).ainvoke("summarise this")

    assert isinstance(response, dict)
    assert {"parsed", "raw", "parsing_error"} <= set(response)
    assert response["parsing_error"] is None

    parsed = response["parsed"]
    assert isinstance(parsed, ArticleSummary)
    assert parsed.title_local == "Yeni bir model duyuruldu"
    assert parsed.importance == 4
    assert parsed.tags == ["openai", "models"]


async def test_the_raw_message_still_carries_the_token_counts(
    keyed_settings: Settings,
) -> None:
    """Costing a run depends on this and on nothing else.

    `usage_from_message` warns and returns zero when it cannot read the counts,
    and that failure is silent all the way to the screen: every row costs $0.000
    while the money leaves.
    """
    response = await _structured(keyed_settings, ANSWER).ainvoke("summarise this")

    usage = usage_from_message(response["raw"])
    assert usage.tokens_in == TOKENS_IN
    assert usage.tokens_out == TOKENS_OUT


async def test_the_request_asks_for_a_json_schema_and_carries_no_tools(
    keyed_settings: Settings,
) -> None:
    """Which mechanism the library picks decides what a recorded answer must
    look like, and it has picked structured outputs rather than function
    calling. A release that moved back to tools would make every answer above
    parse to None with no error, which is the failure the last test pins."""
    requests: list[dict[str, Any]] = []
    await _structured(keyed_settings, ANSWER, requests).ainvoke("summarise this")

    assert len(requests) == 1
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert "tools" not in requests[0]


async def test_a_schema_violation_raises_rather_than_becoming_parsing_error(
    keyed_settings: Settings,
) -> None:
    """The SDK parses eagerly, so pydantic's refusal propagates.

    Worth pinning because it is the opposite of what `include_raw=True` reads
    like it promises. Both call sites wrap the invoke in `except Exception` and
    turn it into one error entry, so a single bad answer costs the branch and
    not the run - but only because they do.
    """
    violating = ANSWER.replace('"importance": 4', '"importance": 99')

    with pytest.raises(Exception) as caught:
        await _structured(keyed_settings, violating).ainvoke("summarise this")
    assert "importance" in str(caught.value)


async def test_an_empty_answer_arrives_as_parsed_none_with_something_to_log(
    keyed_settings: Settings,
) -> None:
    """A model that answers with no content: nothing parsed, and a reason.

    This is the branch `summarize.py` logs as `unparsable model output`, and the
    reason is what makes that log line worth having.
    """
    response = await _structured(keyed_settings, None).ainvoke("summarise this")

    assert response["parsed"] is None
    assert response["parsing_error"] is not None


async def test_a_tool_call_answer_is_the_silent_shape(keyed_settings: Settings) -> None:
    """Nothing parsed, and nothing said about why.

    This is what the previous mechanism's answer looks like arriving at the
    current one, so it is what a library release that moved back to function
    calling would produce on every branch of the fan-out at once. It is also the
    argument for both call sites branching on `parsed is None`: branching on
    `parsing_error` reads this as a success and then fails on the attribute
    access, one story at a time, with no error entry naming the cause.
    """
    tool_shaped = _completion(None)
    tool_shaped["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call_recorded",
            "type": "function",
            "function": {"name": "ArticleSummary", "arguments": ANSWER},
        }
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tool_shaped)

    client = make_llm(
        "gpt-5.6-luna",
        keyed_settings,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ).with_structured_output(ArticleSummary, include_raw=True)
    response = await client.ainvoke("summarise this")

    assert response["parsed"] is None
    assert response["parsing_error"] is None
