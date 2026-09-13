from app.agent.workflow import Workflow
from app.domain.models import RunRequest
from app.observability.store import RunStore


async def test_metric_typo_with_known_alias(settings, tmp_path) -> None:
    store = RunStore(tmp_path / "state.db")
    run = await Workflow(settings, store).run(RunRequest(question="CVR 点击转化律的口径是什么"))
    assert run.status == "succeeded"
    assert any(citation.chunk_id == "metrics.cvr" for citation in run.answer.citations)
    assert not any(event.event == "model" for event in store.events(run.run_id))
