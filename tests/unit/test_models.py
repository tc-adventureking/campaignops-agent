import json
from datetime import date

import httpx
import pytest

from app.agent.planning import TaskContext, demo_plan
from app.agent.prompts import system_prompt
from app.agent.runners import OpenAICompatibleRunner
from app.domain.models import AppError, ErrorCode
from app.settings import Settings

CONTEXT = TaskContext("query", date(2026, 8, 23), date(2026, 8, 29), date(2026, 8, 16), None, [])


async def test_model_tool_call(settings: Settings) -> None:
    plan = demo_plan("CTR", CONTEXT)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["tools"][0]["function"]["name"] == "submit_sql"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "submit_sql",
                                        "arguments": plan.model_dump_json(),
                                    }
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
            },
        )

    runner = OpenAICompatibleRunner(
        settings.model_copy(update={"model_api_key": Settings(model_api_key="fake").model_api_key}),
        httpx.MockTransport(respond),
    )
    for question in ("CTR", "点击率", "查询点击率"):
        assert await runner.plan(question, CONTEXT, []) == plan
    assert runner.metrics.input_tokens == 100
    assert runner.metrics.cache_hit_tokens == 80
    assert runner.metrics.cache_miss_tokens == 20
    assert all(body["messages"][0]["content"] == system_prompt() for body in requests)
    assert len({body["messages"][1]["content"] for body in requests}) == 3


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("timeout", ErrorCode.MODEL_TIMEOUT),
        ("unavailable", ErrorCode.MODEL_UNAVAILABLE),
        ("no_tool", ErrorCode.MODEL_TOOL_UNSUPPORTED),
        ("bad_json", ErrorCode.MODEL_INVALID),
    ],
)
async def test_model_errors(settings: Settings, case: str, expected: ErrorCode) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if case == "timeout":
            raise httpx.ReadTimeout("sensitive path and token", request=request)
        if case == "unavailable":
            return httpx.Response(503, text="sensitive provider failure")
        if case == "no_tool":
            return httpx.Response(200, json={"choices": [{"message": {"content": "no tools"}}]})
        return httpx.Response(200, text="bad json")

    runner = OpenAICompatibleRunner(
        settings.model_copy(update={"model_api_key": Settings(model_api_key="fake").model_api_key}),
        httpx.MockTransport(respond),
    )
    with pytest.raises(AppError) as error:
        await runner.plan("CTR", CONTEXT, [])
    assert error.value.info.code == expected
    assert "sensitive" not in str(error.value)
