from html.parser import HTMLParser

import pytest

from app.agent.report import diagnostic_facts, render
from app.api.presentation import compile_markdown
from app.domain.models import Analysis, Answer, QueryData
from app.tools.analysis import analyze


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.links += [value for key, value in attrs if key == "href"]
        assert not any(key.startswith("on") for key, _ in attrs)


def test_markdown_supports_readable_report_elements():
    html = compile_markdown(
        "# 标题\n\n**结论**\n\n|指标|本期|\n|---|---:|\n|CVR|2%|\n\n- 建议\n\n> 引用\n\n```sql\nSELECT 1 < 2\n```\n\n[来源](/v1/knowledge/metrics.cvr)"
    )
    parsed = Elements()
    parsed.feed(html)
    assert {"h1", "strong", "table", "thead", "tbody", "li", "blockquote", "pre", "code"} <= set(
        parsed.tags
    )
    assert "SELECT 1 &lt; 2" in html
    assert parsed.links == ["/v1/knowledge/metrics.cvr"]


@pytest.mark.parametrize(
    "payload",
    [
        "<script>window.pwned=1</script>",
        '<img src=x onerror="window.pwned=1">',
        '<svg onload="window.pwned=1"></svg>',
        "[点击](javascript:alert%281%29)",
        "[点击](JaVaScRiPt:alert%281%29)",
        "[点击](data:text/html;base64,PHNjcmlwdD4=)",
        "[点击](vbscript:msgbox%281%29)",
        "![图片](https://example.com/tracking.png)",
        "```html\n<img src=x onerror=alert(1)>\n```",
    ],
)
def test_markdown_cannot_activate_untrusted_html_or_urls(payload):
    parsed = Elements()
    parsed.feed(compile_markdown(payload))
    assert not {"script", "img", "svg", "iframe", "object", "style"} & set(parsed.tags)
    assert all(url.startswith("https://") for url in parsed.links)


def test_query_report_uses_table_and_preserves_zero_and_null():
    query = QueryData(
        columns=["campaign_id", "cvr", "spend"],
        column_types=["INTEGER", "DOUBLE", "DOUBLE"],
        rows=[{"campaign_id": 3, "cvr": None, "spend": 0}],
        row_count=1,
        query_hash="hash",
        normalized_sql="SELECT 1",
    )
    result = render(Answer(conclusion="查询完成", evidence=[]), "demo", query)
    assert "| Campaign | 转化率 CVR | 消耗（元） |" in result
    assert "| 3 | — | 0.00 |" in result
    assert "离线规则演示" in result
    assert '"campaign_id":' not in result


def test_diagnostic_report_displays_relative_change_without_mutating_facts():
    analysis = Analysis(
        current={"cvr": 0.023456},
        previous={"cvr": 0.04},
        changes={"cvr": {"relative": -0.4136}},
        root_causes=[],
        contributions={},
        sufficient_data=True,
        limitations=[],
    )
    answer = Answer(conclusion="结果", evidence=[], analysis=analysis, facts=["original evidence"])
    before = answer.model_dump()
    result = render(answer, "youtu")
    assert "| 转化率 CVR | 2.35% | 4.00% | -41.36% |" in result
    assert answer.model_dump() == before


def test_unobserved_diagnostic_periods_are_explicit_in_markdown_and_facts():
    analysis = analyze([], 7).data
    assert analysis is not None
    answer = Answer(
        conclusion="数据不足，无法可靠诊断。",
        evidence=[],
        analysis=analysis,
        facts=diagnostic_facts(analysis, "empty-query"),
    )
    result = render(answer, "demo")
    assert "| 曝光量 | 无数据 | 无数据 | — |" in result
    assert "| 消耗（元） | 无数据 | 无数据 | — |" in result
    assert all("本期=无数据；上期=无数据" in fact for fact in answer.facts)
    assert all("[SQL empty-query]" in fact for fact in answer.facts)


def test_diagnostic_report_distinguishes_observed_zero_from_absent_period():
    analysis = analyze([], 7).data
    assert analysis is not None
    analysis.current.update(impressions=0, clicks=0, spend=0, conversions=0)
    answer = Answer(conclusion="数据不足", evidence=[], analysis=analysis)
    result = render(answer, "demo")
    assert "| 曝光量 | 0 | 无数据 | — |" in result
    assert "| 消耗（元） | 0.00 | 无数据 | — |" in result
    assert "| 点击率 CTR | — | 无数据 | — |" in result
    assert "本期=0；上期=无数据" in diagnostic_facts(analysis, "zero-query")[0]
