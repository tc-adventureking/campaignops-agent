import asyncio
import logging
import os
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.agent.planning import TaskContext, demo_plan, model_context
from app.agent.prompts import system_prompt as prompt
from app.domain.models import AppError, ErrorCode, Evidence, Metrics, SQLPlan
from app.observability.model_usage import CacheUsageCollector, cache_counts
from app.settings import Settings

YOUTU_COMMIT = "c2caa539f4c95ae1c39ed24dc8a99cb3651e1d5d"


class AgentRunner(Protocol):
    metrics: Metrics

    async def plan(
        self, question: str, context: TaskContext, evidence: list[Evidence]
    ) -> SQLPlan: ...


class DemoRunner:
    def __init__(self) -> None:
        self.metrics = Metrics()

    async def plan(self, question: str, context: TaskContext, evidence: list[Evidence]) -> SQLPlan:
        return demo_plan(question, context)


class OpenAICompatibleRunner:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.metrics = Metrics()

    async def plan(self, question: str, context: TaskContext, evidence: list[Evidence]) -> SQLPlan:
        if not self.settings.model_api_key.get_secret_value():
            raise AppError(ErrorCode.MODEL_UNAVAILABLE, "请在 .env 配置 MODEL_API_KEY")
        payload = {
            "model": self.settings.model_name,
            "temperature": 0,
            "max_tokens": self.settings.model_max_tokens,
            "messages": [
                {"role": "system", "content": prompt(self.settings.prompt_variant)},
                {
                    "role": "user",
                    "content": model_context(question, context, [e.model_dump() for e in evidence]),
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_sql",
                        "description": "提交受控只读 SQL 查询计划",
                        "parameters": SQLPlan.model_json_schema(),
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "submit_sql"}},
            "parallel_tool_calls": False,
        }
        # DeepSeek thinking must be disabled when forcing a single tool call.
        if "deepseek.com" in self.settings.model_base_url:
            payload["thinking"] = {"type": "disabled"}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.settings.model_timeout_seconds,
                    connect=self.settings.model_connect_timeout_seconds,
                ),
                transport=self.transport,
            ) as client:
                response = await client.post(
                    self.settings.model_base_url.rstrip("/") + "/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + self.settings.model_api_key.get_secret_value()
                    },
                )
                if response.status_code in (400, 422):
                    raise AppError(
                        ErrorCode.MODEL_TOOL_UNSUPPORTED,
                        "模型不支持当前工具调用协议或参数，请运行 model_probe",
                    )
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    raise AppError(ErrorCode.MODEL_INVALID, "模型响应超过限制")
                body = response.json()
            usage = body.get("usage", {})
            self.metrics = Metrics(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                **cache_counts(usage),
            )
            calls = body["choices"][0]["message"].get("tool_calls", [])
            if not calls:
                raise AppError(ErrorCode.MODEL_TOOL_UNSUPPORTED, "模型未调用 submit_sql 工具")
            if len(calls) != 1 or calls[0]["function"]["name"] != "submit_sql":
                raise AppError(ErrorCode.MODEL_INVALID, "模型工具调用数量或名称不符合契约")
            return SQLPlan.model_validate_json(calls[0]["function"]["arguments"])
        except AppError:
            raise
        except httpx.TimeoutException:
            raise AppError(ErrorCode.MODEL_TIMEOUT, "模型调用超时", True) from None
        except httpx.HTTPStatusError as exc:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "模型接口返回错误，请检查配置与服务状态",
                exc.response.status_code in {408, 429, 500, 502, 503, 504},
            ) from None
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE, "模型接口不可用，请检查地址、模型 ID、密钥与额度", True
            ) from None
        except httpx.HTTPError:
            raise AppError(ErrorCode.MODEL_UNAVAILABLE, "模型请求失败") from None
        except (ValueError, ValidationError, KeyError, TypeError, IndexError):
            raise AppError(ErrorCode.MODEL_INVALID, "模型响应不符合 SQLPlan 契约") from None


class YoutuRunner:
    """Only this adapter imports upstream framework types; no shell or file tool is loaded."""

    def __init__(
        self,
        settings: Settings,
        model: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.model = model
        self.transport = transport
        self.metrics = Metrics()

    async def plan(self, question: str, context: TaskContext, evidence: list[Evidence]) -> SQLPlan:
        if not self.settings.model_api_key.get_secret_value() and self.model is None:
            raise AppError(ErrorCode.MODEL_UNAVAILABLE, "请在 .env 配置 MODEL_API_KEY")
        try:
            # Upstream evaluates this default at import time; actual model config is explicit below.
            os.environ.setdefault("UTU_LLM_MODEL", "configured-by-campaignops")
            os.environ.setdefault("UTU_LLM_TYPE", "chat.completions")
            os.environ["PHOENIX_ENDPOINT"] = ""
            os.environ["PHOENIX_PROJECT_NAME"] = ""
            os.environ["UTU_DB_URL"] = ""
            os.environ.setdefault("OPENAI_AGENTS_DONT_LOG_MODEL_DATA", "1")
            os.environ.setdefault("OPENAI_AGENTS_DONT_LOG_TOOL_DATA", "1")
            from agents import (
                ModelSettings,
                OpenAIChatCompletionsModel,
                RunHooks,
                function_tool,
                set_tracing_disabled,
            )
            from openai import AsyncOpenAI
            from utu.agents import SimpleAgent
            from utu.config import AgentConfig

            set_tracing_disabled(True)
            # Upstream installs a DEBUG file handler and logs tool arguments. Use our redacted trace only.
            upstream_logger = logging.getLogger("utu")
            for handler in upstream_logger.handlers[:]:
                handler.close()
                upstream_logger.removeHandler(handler)
            upstream_logger.addHandler(logging.NullHandler())
            upstream_logger.propagate = False
            captured: list[SQLPlan] = []

            @function_tool
            def submit_sql(plan: SQLPlan) -> str:
                """Submit the read-only SQL plan once. All plan fields are required."""
                if captured:
                    raise AppError(ErrorCode.LIMIT, "检测到重复工具调用，运行已中止")
                captured.append(plan)
                return "SQL plan captured for validation."

            cache_usage = CacheUsageCollector()
            client = AsyncOpenAI(
                api_key=self.settings.model_api_key.get_secret_value() or "offline-test",
                base_url=self.settings.model_base_url,
                timeout=self.settings.model_timeout_seconds,
                max_retries=0,
                http_client=httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        self.settings.model_timeout_seconds,
                        connect=self.settings.model_connect_timeout_seconds,
                    ),
                    transport=self.transport,
                    event_hooks={"response": [cache_usage.on_response]},
                ),
            )
            model = self.model or OpenAIChatCompletionsModel(
                model=self.settings.model_name, openai_client=client
            )
            config = AgentConfig.model_validate(
                {
                    "max_turns": 2,
                    "env": {"name": "base"},
                    "context_manager": {"name": "dummy"},
                    "model": {"model_provider": {"model": self.settings.model_name}},
                }
            )
            extra_body = (
                {"thinking": {"type": "disabled"}}
                if "deepseek.com" in self.settings.model_base_url
                else None
            )
            agent = SimpleAgent(
                config=config,
                name="CampaignOpsSQLPlanner",
                instructions=prompt(self.settings.prompt_variant),
                model=model,
                model_settings=ModelSettings(
                    temperature=0,
                    max_tokens=self.settings.model_max_tokens,
                    tool_choice="required",
                    parallel_tool_calls=False,
                    extra_body=extra_body,
                ),
                tools=[submit_sql],
                tool_use_behavior="stop_on_first_tool",
            )
            agent.run_hooks = RunHooks()
            try:
                async with asyncio.timeout(self.settings.model_timeout_seconds), agent:
                    recorder = await agent.run(
                        model_context(question, context, [e.model_dump() for e in evidence]),
                        log_to_db=False,
                    )
                    usage = recorder.get_run_result().context_wrapper.usage
                    self.metrics = Metrics(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        **cache_usage.totals(),
                    )
            finally:
                await client.close()
            if not captured:
                raise AppError(
                    ErrorCode.MODEL_TOOL_UNSUPPORTED, "Youtu-Agent 未调用 submit_sql 工具"
                )
            return captured[0]
        except AppError:
            raise
        except ImportError:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE, "请运行 uv sync --extra youtu 安装固定版本框架"
            ) from None
        except TimeoutError:
            raise AppError(ErrorCode.MODEL_TIMEOUT, "Youtu-Agent 模型调用超时", True) from None
        except Exception as exc:
            name = type(exc).__name__
            if "Timeout" in name:
                raise AppError(ErrorCode.MODEL_TIMEOUT, "模型调用超时", True) from None
            if name in {"MaxTurnsExceeded", "ModelBehaviorError"}:
                raise AppError(
                    ErrorCode.MODEL_TOOL_UNSUPPORTED, "模型未在限定轮次内完成有效工具调用"
                ) from None
            if name == "ValidationError":
                raise AppError(ErrorCode.MODEL_INVALID, "Youtu-Agent 响应不符合契约") from None
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "Youtu-Agent 调用失败，请运行 model_probe 检查配置",
                name == "APIConnectionError"
                or getattr(exc, "status_code", None) in {408, 429, 500, 502, 503, 504},
            ) from None


def build_runner(settings: Settings) -> AgentRunner:
    if settings.agent_mode == "demo":
        return DemoRunner()
    if settings.agent_mode == "openai":
        return OpenAICompatibleRunner(settings)
    return YoutuRunner(settings)
