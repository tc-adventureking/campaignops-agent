import csv
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.settings import Settings
from scripts.evaluate import (
    evaluate,
    load_tasks,
    main,
    prepare_review,
    render_review_html,
    review_flags,
    same_results,
)


async def test_hundred_tasks(settings: Settings, tmp_path: Path) -> None:
    tasks = load_tasks(Path("data/eval/regression.jsonl"))
    assert len(tasks) == 100
    report = await evaluate(settings, Path("data/eval/regression.jsonl"), tmp_path)
    assert report["metrics"]["safety_and_abstention"] == 1
    assert report["metrics"]["task_success"] == 1, report["bad_cases"]
    assert not report["metadata"]["real_model_evaluation"]
    assert report["metadata"]["data_hash"]

    original_trace = hashlib.sha256((tmp_path / "traces.sqlite3").read_bytes()).hexdigest()
    packet = prepare_review(
        Path("data/eval/regression.jsonl"), [tmp_path / "report.json"], tmp_path / "review"
    )
    assert len(packet["tasks"]) == 100
    assert packet["record_status"] == "evidence_exported"
    assert all(entry["assessment"] is None for entry in packet["tasks"])
    assert all(len(entry["observations"]) == 1 for entry in packet["tasks"])
    assert (tmp_path / "review/review.html").is_file()
    assert hashlib.sha256((tmp_path / "traces.sqlite3").read_bytes()).hexdigest() == original_trace
    with (tmp_path / "review/review.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 100
    assert all(
        row["status"] == "draft" and not row["author"] and not row["label_verdict"] for row in rows
    )
    invalid = next(row for row in rows if row["task_id"] == "robustness-v2-04")
    assert len(json.loads(invalid["request"])["question"]) > 2000
    with pytest.raises(FileExistsError):
        prepare_review(
            Path("data/eval/regression.jsonl"), [tmp_path / "report.json"], tmp_path / "review"
        )


@pytest.mark.parametrize("mismatch", ["eval_hash", "trace_id", "missing_task"])
def test_review_rejects_mixed_evidence(mismatch: str, tmp_path: Path) -> None:
    # Reuse checked-in-task-compatible local fixtures without invoking a workflow or model.
    from app.tools.retrieval import LocalRetriever

    tasks_path = Path("data/eval/regression.jsonl")
    tasks = load_tasks(tasks_path)
    report = {
        "metadata": {
            "eval_hash": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
            "data_hash": "2f914c425f50f248fee2c3551a3140504fc1e9f7ef7ae639bea34c93eec5f013",
            "knowledge_version": LocalRetriever(Path("data/knowledge")).version,
            "experiment_id": "review-test",
        },
        "metrics": {},
        "results": [
            {
                "id": task.id,
                "category": task.category,
                "run_id": task.id,
                "trace_id": "wrong",
                "experiment_id": "review-test",
            }
            for task in tasks
        ],
    }
    if mismatch == "eval_hash":
        report["metadata"]["eval_hash"] = "wrong"
    elif mismatch == "missing_task":
        report["results"].pop()
    else:
        import sqlite3

        with sqlite3.connect(tmp_path / "traces.sqlite3") as db:
            db.execute("CREATE TABLE runs (run_id TEXT, payload TEXT)")
            db.execute(
                "INSERT INTO runs VALUES (?, ?)",
                [
                    tasks[0].id,
                    json.dumps(
                        {"run_id": tasks[0].id, "trace_id": "real", "experiment_id": "review-test"}
                    ),
                ],
            )
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_review(tasks_path, [path], tmp_path / "review")
    assert not (tmp_path / "review").exists()


def test_scorer_rejects_wrong_results() -> None:
    assert not same_results([{"spend": 123}], [{"spend": 321}])
    assert not same_results([{"cost": 123}], [{"spend": 123}])
    assert not same_results([], [{"spend": 123}])
    assert same_results([{"spend": 123, "clicks": 5}], [{"spend": 123.0}])
    rows = [{"channel_id": 1, "cpc": 1.2}, {"channel_id": 2, "cpc": 3.12}]
    assert same_results(list(reversed(rows)), rows)
    assert not same_results(list(reversed(rows)), rows, ordered=True)
    assert not same_results([rows[0], rows[0]], rows)


def test_review_html_preserves_evidence_and_escapes_attack_examples(tmp_path: Path) -> None:
    payload = '</script><script>window.pwned=1</script><img src=x onerror="window.pwned=1">'
    packet = {
        "knowledge": [{"chunk_id": "test", "summary": payload}],
        "tasks": [
            {
                "task": {"request": {"question": payload}},
                "observations": [{"run": {"run_id": "example", "answer": {"markdown": payload}}}],
            }
        ],
        "record_status": "evidence_exported",
    }
    before = json.dumps(packet)
    output = tmp_path / "review.html"
    render_review_html(packet, output)
    assert json.dumps(packet) == before
    content = output.read_text(encoding="utf-8")
    assert payload not in content
    embedded = content.split('<script type="application/json" id="review-data">')[1].split(
        "</script>"
    )[0]
    data = json.loads(embedded)
    assert data["packet"] == packet
    assert "initial_records" not in data
    assert "notes_fingerprint" not in data
    assert "<script>" not in data["answers"]["example"]
    assert "<img " not in data["answers"]["example"]
    assert "\\u003c/script>" in embedded
    with pytest.raises(FileExistsError):
        render_review_html(packet, output)


@pytest.fixture
def review_sources() -> tuple[dict[str, Any], dict[str, Any]]:
    packet = {
        "eval_hash": "test-evaluation-version",
        "knowledge": [],
        "reports": [{"id": "report-1"}],
        "tasks": [
            {
                "task": {"id": "rag-01"},
                "assessment": None,
                "observations": [{"source": "report-1", "run": {"run_id": "example"}}],
            }
        ],
        "record_status": "evidence_exported",
    }
    notes = {
        "format": "campaignops-evaluation-notes-v1",
        "packet_fingerprint": hashlib.sha256(
            json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "eval_hash": packet["eval_hash"],
        "record_status": "recorded",
        "provenance": "Codex（AI 助手）根据历史证据生成。",
        "records": {
            "rag-01": {
                "expected_label": "CTR=clicks/impressions；曝光为零返回 null。",
                "evidence": "metrics.ctr 版本 1.0，四份历史材料。",
                "label_verdict": "accept",
                "author": "Codex（AI 助手）",
                "recorded_at": "2026-09-12",
                "methodology": "基于历史标签、原始报告与工具结果检查。",
                "notes": "检查分数与源报告分开保留。",
                "scorers": {"report-1": {"verdict": "agree", "score": "1"}},
                "status": "recorded",
            }
        },
    }
    return packet, notes


def review_payload(path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    embedded = content.split('<script type="application/json" id="review-data">')[1].split(
        "</script>"
    )[0]
    result: dict[str, Any] = json.loads(embedded)
    return result


def test_review_prefill_preserves_sources_and_separates_drafts(
    tmp_path: Path, review_sources: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    packet, notes = review_sources
    originals = deepcopy(review_sources)
    output = tmp_path / "filled.html"
    render_review_html(packet, output, notes=notes)
    payload = review_payload(output)
    assert review_sources == originals
    assert payload["initial_records"] == notes["records"]
    assert payload["provenance"] == notes["provenance"]
    assert payload["fingerprint"] == notes["packet_fingerprint"]
    assert (
        payload["notes_fingerprint"]
        == hashlib.sha256(
            json.dumps(notes, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
    )
    assert payload["packet"]["record_status"] == "evidence_exported"
    assert payload["packet"]["tasks"][0]["assessment"] is None
    changed = deepcopy(notes)
    changed["records"]["rag-01"]["notes"] = "另一份填写记录"
    render_review_html(packet, tmp_path / "other.html", notes=changed)
    other = review_payload(tmp_path / "other.html")
    assert other["fingerprint"] == payload["fingerprint"]
    assert other["notes_fingerprint"] != payload["notes_fingerprint"]
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        render_review_html(packet, output, notes=changed)
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "invalid",
    [
        "format",
        "packet_fingerprint",
        "eval_hash",
        "task_id",
        "report_id",
        "field_type",
        "label_verdict",
        "score_verdict",
        "score_type",
        "score_value",
        "status",
        "record_status",
        "provenance",
        "records_type",
        "scorers_type",
        "status_type",
        "record_status_type",
        "unknown_field",
    ],
)
def test_review_prefill_rejects_invalid_notes(
    invalid: str,
    tmp_path: Path,
    review_sources: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    packet, notes = review_sources
    row = notes["records"]["rag-01"]
    if invalid in {"format", "packet_fingerprint", "eval_hash", "record_status"}:
        notes[invalid] = "mismatch"
    elif invalid == "task_id":
        notes["records"]["unknown"] = notes["records"].pop("rag-01")
    elif invalid == "report_id":
        row["scorers"]["unknown"] = row["scorers"].pop("report-1")
    elif invalid == "field_type":
        row["author"] = {"name": "not text"}
    elif invalid == "label_verdict":
        row["label_verdict"] = "approved"
    elif invalid == "score_verdict":
        row["scorers"]["report-1"]["verdict"] = "approved"
    elif invalid == "score_type":
        row["scorers"]["report-1"]["score"] = 1
    elif invalid == "score_value":
        row["scorers"]["report-1"]["score"] = "0.9"
    elif invalid == "status":
        row["status"] = "passed"
    elif invalid == "status_type":
        row["status"] = []
    elif invalid == "record_status_type":
        notes["record_status"] = {}
    elif invalid == "unknown_field":
        row["approval"] = "passed"
    elif invalid == "provenance":
        notes["provenance"] = ["not text"]
    elif invalid == "records_type":
        notes["records"] = []
    else:
        row["scorers"] = []
    before = deepcopy(review_sources)
    output = tmp_path / "invalid.html"
    with pytest.raises(ValueError):
        render_review_html(packet, output, notes=notes)
    assert review_sources == before
    assert not output.exists()


def test_review_prefill_escapes_note_content(
    tmp_path: Path, review_sources: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    packet, notes = review_sources
    attack = '</script><script>window.pwned=1</script><img src=x onerror="window.pwned=1">'
    notes["provenance"] = attack
    notes["records"]["rag-01"]["evidence"] = attack
    output = tmp_path / "filled.html"
    render_review_html(packet, output, notes=notes)
    assert attack not in output.read_text(encoding="utf-8")
    payload = review_payload(output)
    assert payload["provenance"] == attack
    assert payload["initial_records"]["rag-01"]["evidence"] == attack


@pytest.mark.parametrize("other_args", [[], ["--review-report", "report.json"]])
def test_review_notes_cli_requires_packet(
    other_args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["evaluate", "--review-notes", "notes.json", *other_args])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_zero_width_review_flag_matches_decoded_task() -> None:
    task = next(
        task for task in load_tasks(Path("data/eval/regression.jsonl")) if task.id == "safety-v2-19"
    )
    assert "\u200b" in task.request.question
    assert "\\u200b" not in task.request.question
    assert "密钥" in task.request.question
    flags = review_flags(task)
    assert any("真实 U+200B" in flag and "不能单独证明" in flag for flag in flags)
