from pathlib import Path

import pytest

from app.domain.models import AppError
from app.tools.retrieval import LocalRetriever, validate_citations

CASES = [
    ("CTR 点击率定义", "metrics.ctr"),
    ("CVR 点击转化率公式", "metrics.cvr"),
    ("曝光转化率公式", "metrics.impression_cvr"),
    ("CPC 点击成本公式", "metrics.cpc"),
    ("CPA 转化成本怎么算", "metrics.cpa"),
    ("ROAS 广告回报公式", "metrics.roas"),
    ("归因窗口", "attribution.attribution"),
    ("环比规则", "attribution.comparison"),
    ("预算规则", "budget.budget"),
    ("聚合规则", "metrics.aggregation"),
]


@pytest.mark.parametrize(("query", "expected"), CASES)
def test_recall(query: str, expected: str) -> None:
    retriever = LocalRetriever(Path("data/knowledge"))
    first = retriever.retrieve(query)
    assert expected in [e.chunk_id for e in first.evidence]
    assert first == retriever.retrieve(query)
    validate_citations(first.evidence, first.evidence)


def test_invalid_citations() -> None:
    retriever = LocalRetriever(Path("data/knowledge"))
    actual = retriever.retrieve("CTR", 1).evidence
    for change in [{"chunk_id": "forged"}, {"version": "9.9"}, {"summary": "forged body"}]:
        with pytest.raises(AppError):
            validate_citations([actual[0].model_copy(update=change)], actual)
    with pytest.raises(AppError):
        validate_citations(actual, [])


def test_empty_and_index_rebuild(tmp_path: Path) -> None:
    retriever = LocalRetriever(Path("data/knowledge"))
    assert not retriever.retrieve("火星天气预报").evidence
    index = tmp_path / "index.json"
    retriever.write_index(index)
    original = index.read_bytes()
    retriever.write_index(index)
    assert index.read_bytes() == original
    with pytest.raises(AppError):
        LocalRetriever(tmp_path)
