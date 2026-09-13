"""Exercise the local workspace in Chromium, using isolated demo data and no paid API."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
from playwright.sync_api import Page, Route, expect, sync_playwright

from scripts.seed_data import seed_data


def no_overflow(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        "Page overflows viewport"
    )


def stage2_checks(page: Page, base: str) -> None:
    keys: list[str] = []
    ids: list[str] = []

    def lose_first_response(route: Route) -> None:
        keys.append(route.request.headers["idempotency-key"])
        response = route.fetch()
        ids.append(response.json()["run_id"])
        if len(keys) == 1:
            route.abort()
        else:
            route.fulfill(response=response)

    page.route(base + "/v1/runs", lose_first_response)
    page.locator("#question").fill("查询最近7天 CTR")
    page.locator("#submit").click()
    expect(page.locator("#error-panel")).to_be_visible()
    page.locator("#retry-button").click()
    expect(page.locator("#result-section")).to_be_visible()
    assert len(keys) == 2 and keys[0] == keys[1] and ids[0] == ids[1]
    page.unroute(base + "/v1/runs", lose_first_response)

    page.locator("#new-run").click()
    page.locator("#question").fill("将 Campaign 3 预算提高20%")
    page.locator("#submit").click()
    expect(page.locator("#result-section")).to_be_visible()
    page.get_by_label("模拟 Campaign", exact=True).select_option("3")
    page.get_by_label("模拟日预算", exact=True).fill("1200")
    page.get_by_label("审批口令", exact=True).fill("offline-ui-test-operator")
    page.get_by_role("button", name="生成模拟提议", exact=True).click()
    expect(page.get_by_role("button", name="批准此提议", exact=True)).to_be_visible()
    expect(page.get_by_label("模拟日预算", exact=True)).to_be_disabled()
    for iteration in range(2):
        page.get_by_role("button", name="批准此提议", exact=True).click()
        execute = page.get_by_role("button", name="执行已批准的模拟变更", exact=True)
        expect(execute).to_be_visible()
        execute.click()
        rollback = page.get_by_role("button", name="提议回滚", exact=True)
        expect(rollback).to_be_visible()
        if iteration == 0:
            rollback.click()
            expect(page.get_by_label("模拟日预算", exact=True)).to_have_value("1000")
    page.get_by_role("button", name="查看审批记录", exact=True).click()
    expect(page.locator("#report-content pre")).to_contain_text("executed")
    for width in (1440, 1024, 768, 390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        no_overflow(page)
    page.goto(base + "/observability")
    expect(page.locator("#runs button").first).to_be_visible()
    page.locator("#runs button").first.click()
    expect(page.locator("#trace")).to_contain_text("trace_id")


def checks(base: str, output: Path) -> dict[str, Any]:
    errors: list[str] = []
    external: list[str] = []
    submissions: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            reduced_motion="reduce",
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def observe(route: Route) -> None:
            if not route.request.url.startswith(base + "/"):
                external.append(route.request.url)
                route.abort()
            else:
                if route.request.method == "POST" and route.request.url == base + "/v1/runs":
                    submissions.append(route.request.url)
                route.continue_()

        page.route("**/*", observe)
        page.goto(base)
        expect(page.locator(".connection.ready")).to_be_visible()
        no_overflow(page)
        page.screenshot(path=output / "desktop-initial.png", full_page=True, animations="disabled")
        page.locator("#submit").click()
        expect(page.locator("#result-section")).to_be_visible(timeout=20000)
        expect(page.locator("#metric-cards .metric-card")).to_have_count(4)
        expect(page.locator("#report-content table tbody tr")).to_have_count(8)
        expect(page.locator("#report-content h1")).to_have_text("投放诊断报告")
        assert len(submissions) == 1
        first_url = page.url
        first_id = first_url.split("?run=", 1)[1]
        first_run = context.request.get(f"{base}/v1/runs/{first_id}").json()
        assert "2.75%" in page.locator("#metric-cards").inner_text()
        page.screenshot(path=output / "desktop-result.png", full_page=True, animations="disabled")
        page.locator("#tab-evidence").click()
        page.locator("#panel-evidence button").first.click()
        expect(page.locator("#content-dialog")).to_be_visible()
        expect(page.locator("#dialog-body .markdown-body h2")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#content-dialog")).not_to_be_visible()
        page.locator("#tab-details").click()
        expect(page.locator("#panel-details")).to_contain_text("缓存命中 Token")
        expect(page.locator("#panel-details")).to_contain_text("未提供")
        page.locator("#tab-report").click()
        page.locator("#copy-report").click()
        assert page.evaluate("navigator.clipboard.readText()") == first_run["answer"]["markdown"]
        with page.expect_download() as pending:
            page.locator("#download-report").click()
        download = pending.value
        download.save_as(output / "example-report.md")
        assert (output / "example-report.md").read_text() == first_run["answer"]["markdown"]

        # Reload must retrieve a run, not submit a new analysis.
        page.reload()
        expect(page.locator("#result-section")).to_be_visible()
        assert len(submissions) == 1
        page.locator("#knowledge-nav").click()
        expect(page.locator(".knowledge-item")).to_have_count(15)
        page.keyboard.press("Escape")

        # Break SSE deliberately: GET polling still finds the final result.
        page.route("**/v1/runs/*/events", lambda route: route.abort())
        page.locator("#new-run").click()
        page.locator(".scenario").nth(2).click()
        page.locator("#submit").click()
        expect(page.locator("#result-section")).to_be_visible(timeout=20000)
        expect(page.locator("#report-content table tbody tr")).to_have_count(3)
        assert len(submissions) == 2
        page.unroute("**/v1/runs/*/events")
        page.locator("#new-run").click()
        page.locator("#question").fill("将 Campaign 3 预算提高 20%")
        page.locator("#submit").click()
        expect(page.locator(".approval-message")).to_be_visible()
        expect(page.locator(".approval-state")).to_contain_text("未执行投放变更")
        expect(page.locator("#copy-report")).to_be_disabled()

        # Surface a structured server failure and enable an explicit retry.
        page.route(
            "**/v1/runs",
            lambda route: route.fulfill(status=502, json={"error": {"message": "模型暂时不可用"}}),
            times=1,
        )
        page.locator("#new-run").click()
        page.locator("#question").fill("查询 CTR")
        page.locator("#submit").click()
        expect(page.locator("#error-panel")).to_contain_text("模型暂时不可用")
        expect(page.locator("#submit")).to_be_enabled()
        page.locator("#retry-button").click()
        expect(page.locator("#result-section")).to_be_visible()

        # Hostile Markdown passes through the real renderer and real UI insertion path.
        hostile = "# 安全排版\n\n<script>window.markdownPwned=1</script>\n\n<img src=x onerror=window.markdownPwned=1>\n\n[危险链接](javascript:alert%281%29)\n\n|指标|值|\n|---|---|\n|CVR|2%|\n\n```html\n<img src=x>\n```\n\n> 引用\n\n- 正常列表\n\n**正常加粗**"
        altered = json.loads(json.dumps(first_run))
        altered["answer"]["markdown"] = hostile
        page.route(f"**/v1/runs/{first_id}", lambda route: route.fulfill(json=altered), times=1)
        page.goto(first_url)
        expect(page.locator("#report-content h1")).to_have_text("安全排版")
        expect(
            page.locator("#report-content script, #report-content img, #report-content a")
        ).to_have_count(0)
        expect(page.locator("#report-content table")).to_have_count(1)
        expect(page.locator("#report-content pre code")).to_contain_text("<img src=x>")
        assert page.evaluate("window.markdownPwned === undefined")

        page.goto(first_url)
        expect(page.locator("#report-content h1")).to_have_text("投放诊断报告")
        for width in (1440, 1024, 768, 390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            no_overflow(page)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=output / "mobile-result.png", full_page=True, animations="disabled")
        page.locator("#history-button").click()
        expect(page.locator("#content-dialog .history-item")).not_to_have_count(0)
        page.keyboard.press("Escape")
        page.locator("#new-run").click()
        page.screenshot(path=output / "mobile-initial.png", full_page=True, animations="disabled")
        stage2_checks(page, base)
        assert not errors, errors
        assert not external, external
        browser.close()
    return {
        "passed": True,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "isolated demo, no model API",
        "checks": [
            "desktop and mobile layout at five widths",
            "Markdown tables and code",
            "real diagnosis and Top-N",
            "sources and knowledge dialog",
            "clipboard and Markdown download",
            "history and reload without resubmission",
            "SSE loss recovered by GET polling",
            "readable approval",
            "structured failure and explicit retry",
            "XSS blocked in real DOM",
            "no external assets",
            "lost POST response reuses idempotency key",
            "approval, execution, rollback and audit",
            "approval parameter display and responsive layout",
            "observability trace drilldown",
        ],
        "page_errors": errors,
        "external_requests": external,
    }


def review_checks(path: Path, output: Path) -> dict[str, Any]:
    """Check the standalone evaluation notes in an isolated browser."""
    errors: list[str] = []
    requests: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                requests.append(request.url)
                if request.url.startswith(("http:", "https:"))
                else None
            ),
        )
        page.goto(path.resolve().as_uri())
        expect(page.locator("#task-list button")).to_have_count(100)
        expect(page.locator("#question")).to_have_text("CTR 点击率的定义")
        assert page.locator("#reference").evaluate("element => element.open")
        page.screenshot(path=output / "review-desktop.png", full_page=True)
        page.locator("#expected_label").fill("浏览器自动化检查草稿。")
        page.locator("#evidence").fill("只验证填写、保存与导出行为。")
        page.locator("#next").click()
        page.locator("#previous").click()
        expect(page.locator("#expected_label")).to_have_value("浏览器自动化检查草稿。")
        page.reload()
        expect(page.locator("#evidence")).to_have_value("只验证填写、保存与导出行为。")
        expect(page.locator("#observations .observation")).to_have_count(4)
        expect(page.locator("#observations .markdown h1")).to_have_count(4)
        page.locator("#label_verdict").select_option("uncertain")
        page.locator("#observations select").nth(0).select_option("uncertain")
        page.locator("#observations select").nth(1).select_option("0.5")
        with page.expect_download() as download:
            page.locator("#export").click()
        notes_path = output / "browser-test-notes.json"
        download.value.save_as(notes_path)
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
        assert len(notes["records"]) == 100
        assert notes["records"]["rag-01"]["status"] == "recorded"
        assert all(
            row["status"] == "draft"
            for task_id, row in notes["records"].items()
            if task_id != "rag-01"
        )
        assert notes["records"]["rag-01"]["scorers"]["report-1"] == {
            "verdict": "uncertain",
            "score": "0.5",
        }
        assert notes["record_status"] == "recorded"
        page.locator("#expected_label").fill("保留这条较新的草稿。")
        page.locator("#import-file").set_input_files(notes_path)
        expect(page.locator("#save-status")).to_contain_text("有冲突")
        expect(page.locator("#expected_label")).to_have_value("保留这条较新的草稿。")
        page.evaluate("localStorage.clear()")
        page.reload()
        page.locator("#import-file").set_input_files(notes_path)
        expect(page.locator("#save-status")).to_contain_text("已导入")
        expect(page.locator("#expected_label")).to_have_value("浏览器自动化检查草稿。")
        imported_provenance = "Browser test fixture generated by automation"
        with_provenance = notes | {"provenance": imported_provenance}
        page.locator("#import-file").set_input_files(
            {
                "name": "with-source.json",
                "mimeType": "application/json",
                "buffer": json.dumps(with_provenance).encode(),
            }
        )
        expect(page.locator("#prefill-provenance")).to_have_text(imported_provenance)
        page.reload()
        expect(page.locator("#prefill-provenance")).to_have_text(imported_provenance)
        with page.expect_download() as source_download:
            page.locator("#export").click()
        source_notes_path = output / "browser-source-notes.json"
        source_download.value.save_as(source_notes_path)
        exported = json.loads(source_notes_path.read_text(encoding="utf-8"))
        assert exported["provenance"] == imported_provenance
        assert exported["format"] == "campaignops-evaluation-notes-v1"
        wrong_packet = notes | {"packet_fingerprint": "wrong"}
        page.locator("#import-file").set_input_files(
            {
                "name": "wrong.json",
                "mimeType": "application/json",
                "buffer": json.dumps(wrong_packet).encode(),
            }
        )
        expect(page.locator("#save-status")).to_contain_text("不匹配")
        page.locator("#category").select_option("sql")
        expect(page.locator("#task-list button")).to_have_count(25)
        expect(page.locator("#reference-evidence table")).to_be_visible()
        page.locator("#category").select_option("")
        page.locator("#search").fill("robustness-v2-04")
        expect(page.locator("#task-list button")).to_have_count(1)
        expect(page.locator("#fault")).to_contain_text("2001")
        for width in (1440, 768, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            no_overflow(page)
        page.locator("#search").fill("rag-01")
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=output / "review-mobile.png", full_page=True)
        page.locator("#search").fill("safety-v2-16")
        expect(page.locator("#question")).to_contain_text("<system>")
        assert not page.locator("#question system").count()
        page.locator("#search").fill("no-such-task")
        expect(page.locator("#empty")).to_be_visible()
        assert not errors, errors
        assert not requests, requests
        browser.close()
    return {
        "passed": True,
        "scope": "standalone evaluation notes; automated browser checks",
        "tasks": 100,
        "reports": 4,
        "page_errors": errors,
        "external_requests": requests,
        "checks": [
            "readable questions and expanded reference answers",
            "navigation/search/category filter",
            "local draft and provenance persistence",
            "notes export/import and mismatch/conflict rejection",
            "exported statuses distinguish recorded entries from blank drafts",
            "SQL tables and rendered Markdown",
            "long inputs at four viewport widths",
            "attack inputs rendered as text",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--review-page", type=Path, help="Check an exported review.html without starting the API"
    )
    args = parser.parse_args()
    output: Path = args.output or Path(
        "artifacts/ui-review" if args.review_page else "artifacts/ui"
    )
    output.mkdir(parents=True, exist_ok=True)
    if args.review_page:
        report = review_checks(args.review_page, output)
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    with TemporaryDirectory(prefix="campaignops-ui-") as directory:
        seed_data(Path(directory))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (output / "server.log").open("w") as logs:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                env=os.environ
                | {
                    "AGENT_MODE": "demo",
                    "MODEL_API_KEY": "",
                    "DATA_DIR": directory,
                    "DATABASE_BACKEND": "duckdb",
                    "REDIS_URL": "",
                    "APPROVAL_API_KEY": "offline-ui-test-operator",
                },
                stdout=logs,
                stderr=logs,
            )
            try:
                deadline = time.monotonic() + 20
                with httpx.Client(timeout=2) as client:
                    while True:
                        try:
                            if client.get(base + "/health/ready").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() > deadline:
                            raise RuntimeError("UI test server did not start")
                        time.sleep(0.1)
                report = checks(base, output)
                (output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
