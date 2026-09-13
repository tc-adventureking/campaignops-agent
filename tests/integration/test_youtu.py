import json
import os
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from app.agent.planning import TaskContext, demo_plan
from app.agent.runners import YoutuRunner
from app.settings import Settings

agents = pytest.importorskip("agents")
os.environ.setdefault("UTU_LLM_TYPE", "chat.completions")
os.environ.setdefault("UTU_LLM_MODEL", "offline-test")
pytest.importorskip("utu")


class FakeToolModel(agents.Model):
    async def get_response(self, system_instructions, input, model_settings, tools, **kwargs):
        from agents.items import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import ResponseFunctionToolCall

        payload = json.loads(input[-1]["content"])
        from datetime import date, timedelta

        start, end = (
            date.fromisoformat(payload["start_date"]),
            date.fromisoformat(payload["end_date"]),
        )
        context = TaskContext(
            payload["intent"], start, end, start - timedelta(days=7), payload["campaign_id"], []
        )
        plan = demo_plan(payload["question_untrusted"], context)
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    id="fc_test",
                    call_id="call_test",
                    name="submit_sql",
                    arguments=json.dumps({"plan": plan.model_dump(mode="json")}),
                    type="function_call",
                )
            ],
            usage=Usage(requests=1, input_tokens=10, output_tokens=20),
            response_id="fake-response",
        )

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


@pytest.mark.youtu
async def test_real_framework_with_fake_model(settings: Settings) -> None:
    from datetime import date

    context = TaskContext("query", date(2026, 8, 23), date(2026, 8, 29), date(2026, 8, 16), 3, [])
    for _ in range(3):
        runner = YoutuRunner(settings, model=FakeToolModel())
        plan = await runner.plan("查询 CVR", context, [])
        assert "SUM(conversions)" in plan.sql
        assert runner.metrics.input_tokens == 10
        assert runner.metrics.cache_hit_tokens is None


@pytest.mark.youtu
async def test_real_sdk_preserves_provider_cache_usage(settings: Settings) -> None:
    context = TaskContext("query", date(2026, 8, 23), date(2026, 8, 29), date(2026, 8, 16), 3, [])
    expected = demo_plan("查询 CVR", context)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        assert "固定数据库 schema" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_test",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_sql",
                                        "arguments": json.dumps(
                                            {"plan": expected.model_dump(mode="json")}
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
            },
        )

    runner = YoutuRunner(
        settings.model_copy(update={"model_api_key": SecretStr("fake")}),
        transport=httpx.MockTransport(respond),
    )
    assert await runner.plan("查询 CVR", context, []) == expected
    assert runner.metrics.input_tokens == 100
    assert runner.metrics.cache_hit_tokens == 80
    assert runner.metrics.cache_miss_tokens == 20
