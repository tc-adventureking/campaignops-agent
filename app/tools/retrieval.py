import hashlib
import json
import math
import re
from pathlib import Path
from typing import Protocol

from app.agent.reliability import cancellation
from app.domain.models import AppError, ErrorCode, Evidence, ToolResult


def tokens(text: str) -> set[str]:
    result = set(re.findall(r"[a-z][a-z0-9_]*", text.lower()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(part[i : i + 2] for i in range(len(part) - 1))
    return result


def rewrite_query(query: str) -> str:
    query = re.sub(r"\d{4}-\d{2}-\d{2}|(?:最近|近)\d+天|campaign\s*\d+", " ", query, flags=re.I)
    query = re.sub(r"是什么|什么是|如何计算|怎么算|请问|请|查询|的", " ", query)
    return query.strip()


class Retriever(Protocol):
    version: str

    def retrieve(self, query: str, top_k: int = 5) -> ToolResult[list[Evidence]]: ...


class LocalRetriever:
    def __init__(self, directory: Path):
        self.directory = directory
        self.chunks: list[Evidence] = []
        self.titles: dict[str, str] = {}
        digest = hashlib.sha256()
        for path in sorted(directory.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            digest.update(content.encode())
            lines = content.splitlines()
            metadata = json.loads(lines[0].removeprefix("<!-- ").removesuffix(" -->"))
            self.titles[metadata["doc_id"]] = metadata["title"]
            starts = [
                (i, re.search(r"\{#([^}]+)\}", line))
                for i, line in enumerate(lines)
                if line.startswith("## ")
            ]
            for index, (start, match) in enumerate(starts):
                if not match:
                    raise AppError(ErrorCode.RETRIEVAL, "知识片段缺少稳定 ID")
                end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
                self.chunks.append(
                    Evidence(
                        kind="document",
                        doc_id=metadata["doc_id"],
                        chunk_id=match.group(1),
                        version=metadata["version"],
                        source=f"{path.as_posix()}#L{start + 1}",
                        line_start=start + 1,
                        line_end=end,
                        summary="\n".join(lines[start:end]).strip(),
                    )
                )
        if not self.chunks or len({c.chunk_id for c in self.chunks}) != len(self.chunks):
            raise AppError(ErrorCode.RETRIEVAL, "知识库为空或片段 ID 重复")
        self.version = digest.hexdigest()

    def retrieve(self, query: str, top_k: int = 5) -> ToolResult[list[Evidence]]:
        query_tokens = tokens(rewrite_query(query))
        corpus = [
            tokens(self.titles.get(chunk.doc_id or "", "") + " " + chunk.summary)
            for chunk in self.chunks
        ]
        weights = {
            token: math.log(1 + len(corpus) / (1 + sum(token in doc for doc in corpus)))
            for token in query_tokens
        }
        ranked = []
        for chunk in self.chunks:
            scope = cancellation.get()
            if scope:
                scope.check()
            doc_tokens = tokens(self.titles.get(chunk.doc_id or "", "") + " " + chunk.summary)
            overlap = query_tokens & doc_tokens
            score = sum(weights[token] for token in overlap) / math.sqrt(
                max(1, len(query_tokens) * len(doc_tokens))
            )
            # Exact metric aliases avoid confusing click CVR with impression CVR.
            key = (chunk.chunk_id or "").split(".")[-1]
            if key in query_tokens:
                score += 1
            if "曝光转化率" in query and key == "impression_cvr":
                score += 2
            elif "转化率" in query and "曝光转化率" not in query and key == "cvr":
                score += 1
            if score >= 0.06:
                ranked.append(chunk.model_copy(update={"score": round(score, 6)}))
        ranked.sort(key=lambda c: (-(c.score or 0), c.chunk_id or ""))
        selected = ranked[: max(1, min(top_k, 10))]
        return ToolResult(status="ok", data=selected, evidence=selected)

    def write_index(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": self.version,
                    "retriever": "lexical-cjk-v1",
                    "chunks": [c.model_dump() for c in self.chunks],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def validate_citations(citations: list[Evidence], retrieved: list[Evidence]) -> None:
    valid = {(item.chunk_id, item.version): item for item in retrieved if item.kind == "document"}
    for citation in citations:
        actual = valid.get((citation.chunk_id, citation.version))
        if not actual or citation.model_dump(exclude={"score"}) != actual.model_dump(
            exclude={"score"}
        ):
            raise AppError(ErrorCode.CITATION, "引用不存在、版本不匹配或未被本次检索返回")
