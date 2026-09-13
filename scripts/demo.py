import argparse
import asyncio

from app.agent.workflow import Workflow
from app.domain.models import RunRequest
from app.observability.store import RunStore
from app.settings import Settings


async def demo(mode: str) -> None:
    settings = Settings(agent_mode=mode)  # type: ignore[arg-type]
    workflow = Workflow(
        settings, RunStore(settings.state_path, (settings.model_api_key.get_secret_value(),))
    )
    for question in (
        "诊断 Campaign 3 最近7天 CVR 下滑的原因",
        "诊断 Campaign 6 最近7天渠道成本异常",
        "将 Campaign 3 预算提高 20%",
    ):
        result = await workflow.run(RunRequest(question=question))
        print(result.answer.markdown if result.answer else result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["demo", "openai", "youtu"], default="demo")
    args = parser.parse_args()
    asyncio.run(demo(args.mode))


if __name__ == "__main__":
    main()
