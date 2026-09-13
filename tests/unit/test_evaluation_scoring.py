from pathlib import Path

import pytest

from app.domain.models import Answer, RootCause
from app.tools.retrieval import LocalRetriever
from scripts.evaluate import load_tasks, score_diagnosis, score_rag


def test_rag_requires_both_formula_and_attribution_sources() -> None:
    expected = {
        "chunk_id": "metrics.roas",
        "required_chunk_ids": ["metrics.roas", "attribution.attribution"],
    }
    chunks = LocalRetriever(Path("data/knowledge")).chunks
    formula = next(chunk for chunk in chunks if chunk.chunk_id == "metrics.roas")
    attribution = next(chunk for chunk in chunks if chunk.chunk_id == "attribution.attribution")
    answer = Answer(
        conclusion="ROAS 的转化价值使用点击后 7 天归因。", evidence=[], citations=[formula]
    )
    score, recall, details = score_rag(expected, answer)
    assert (score, recall) == (0, 0.5)
    assert "attribution.attribution" in details
    answer.citations.append(attribution)
    assert score_rag(expected, answer)[:2] == (1, 1)
    # Existing single-source tasks retain their original scoring contract.
    assert score_rag({"chunk_id": "metrics.roas"}, answer)[:2] == (1, 1)
    assert score_rag(expected, None)[:2] == (0, 0)


@pytest.mark.parametrize(
    "text",
    [
        "CTR=clicks/impressions；分母为零返回 null。",
        "点击为零时，CTR 为 null。",
        "点击为零且曝光大于零时，CTR 为 0。",
        "曝光为零时，CTR 为 null。",
    ],
)
def test_zero_clicks_requires_both_explicit_branches(text: str) -> None:
    task = next(
        task for task in load_tasks(Path("data/eval/regression.jsonl")) if task.id == "rag-v2-12"
    )
    citations = LocalRetriever(Path("data/knowledge")).retrieve("CTR").evidence
    answer = Answer(conclusion=text, evidence=citations, citations=citations)
    score, recall, details = score_rag(task.expected, answer)
    assert score == 0 and recall == 1
    assert "zero_" in details
    # The cited document already contains the branches, but only the answer body counts.
    answer.conclusion = "根据点击率口径区分曝光量。"
    answer.facts = ["点击为零且曝光大于零时，CTR 为 0；曝光为零时，CTR 为 null。"]
    assert score_rag(task.expected, answer)[:2] == (1, 1)


@pytest.mark.parametrize("task_id", ["diagnosis-v2-12", "diagnosis-v2-18", "diagnosis-v2-24"])
@pytest.mark.parametrize("code", ["cpc_rise", "cvr_drop"])
def test_channel_anomaly_secondary_cause_receives_partial_credit(task_id: str, code: str) -> None:
    task = next(
        task for task in load_tasks(Path("data/eval/regression.jsonl")) if task.id == task_id
    )
    cause = RootCause(
        code=code,
        title=code,
        contribution=1,
        confidence=0.8,
        supporting_evidence=[],
        counter_evidence=[],
        confidence_components={},
    )
    answer = Answer(conclusion="渠道异常的次要信号", evidence=[], root_causes=[cause])
    assert score_diagnosis(task.expected, answer) == 0.5
    answer.root_causes[0] = cause.model_copy(update={"code": "channel_anomaly"})
    assert score_diagnosis(task.expected, answer) == 1
    answer.root_causes[0] = cause.model_copy(update={"code": "traffic_drop"})
    assert score_diagnosis(task.expected, answer) == 0
