"""Stable shared prefix. Request-specific data belongs only in model_context()."""

import hashlib
import json
from pathlib import Path

from app.domain.semantics import SCHEMA, metric_catalog
from app.guardrails.sql import ALLOWED_FUNCTIONS


def system_prompt(variant: str = "semantic") -> str:
    schema_details = json.loads(Path("configs/schema.json").read_text(encoding="utf-8"))
    schema = {
        "version": schema_details["version"],
        "tables": {
            table: {
                column: {
                    "type": kind,
                    "description": schema_details["tables"][table][column]["description"],
                }
                for column, kind in columns.items()
            }
            for table, columns in SCHEMA.items()
        },
        "relations": schema_details["relations"],
    }
    instructions = Path("configs/prompt.txt").read_text(encoding="utf-8").strip()
    if variant == "plain":
        # Date, tool, diagnosis and security contracts stay identical across arms.
        before, rest = instructions.split("三、指标计算规范", 1)
        _, after = rest.split("四、时间范围和对象范围", 1)
        instructions = before + "四、时间范围和对象范围" + after
    blocks = [instructions]
    sections: list[tuple[str, object]] = [
        ("固定数据库 schema 与业务字段说明", schema if variant == "semantic" else SCHEMA)
    ]
    if variant == "semantic":
        sections.append(("固定指标语义", metric_catalog()))
    sections.append(("允许的 SQL 函数", sorted(ALLOWED_FUNCTIONS)))
    if variant == "semantic":
        sections.append(
            ("固定查询示例", json.loads(Path("configs/few_shots.json").read_text(encoding="utf-8")))
        )
    for title, data in sections:
        blocks.append(title + "\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
    return "\n\n".join(blocks)


def prompt_hash(variant: str = "semantic") -> str:
    return hashlib.sha256(system_prompt(variant).encode()).hexdigest()
