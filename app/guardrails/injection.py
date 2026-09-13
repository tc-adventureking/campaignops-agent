"""Defense in depth; SQL capabilities and approval checks remain the authority."""

import re
import unicodedata

from app.domain.models import AppError, ErrorCode

PATTERN = re.compile(
    r"忽略.*(?:规则|指令|提示)|绕过|系统提示|密钥|泄露|读取.*文件|/etc/|\.env\b|"
    r"api[_ -]?key|password|secret|ignore.*(?:instructions|rules)|system\s*prompt|"
    r"reveal.*(?:prompt|credentials)|you are now|developer\s*(?:message|:)|"
    r"\[/?INST\]|<\|(?:im_start|system)|</?(?:system|developer)>|"
    r"(?:drop|alter|truncate)\s+table|delete\s+from|insert\s+into|update\s+\w+\s+set|"
    r"read_csv|read_parquet|pg_read_file|information_schema|pg_catalog|"
    r"execute.*(?:shell|command)|curl\s+https?://|(?:跳过|无需|伪造).*(?:审批|批准)",
    re.I | re.S,
)


def suspicious(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    return bool(PATTERN.search(normalized))


def require_data(text: str, source: str) -> None:
    # A safety policy may name a secret or say "禁止 Prompt 泄露". Look for actionable
    # directives in external data, not isolated security vocabulary.
    control = re.compile(
        r"忽略.*(?:规则|指令|提示)|ignore.*(?:instructions|rules)|you are now|"
        r"developer\s*(?:message|:)|\[/?INST\]|<\|(?:im_start|system)|</?(?:system|developer)>|"
        r"(?:输出|打印|发送|泄露|读取|reveal|print|send).{0,30}(?:密钥|密码|api.?key|password|secret|system.prompt)|"
        r"(?:drop|alter|truncate)\s+table|delete\s+from|insert\s+into|update\s+\w+\s+set|"
        r"read_csv|read_parquet|pg_read_file|execute.*(?:shell|command)|curl\s+https?://|"
        r"(?:跳过|无需|伪造).*(?:审批|批准)",
        re.I | re.S,
    )
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    if control.search(normalized):
        raise AppError(ErrorCode.FORBIDDEN, f"已拦截 {source} 中的可疑指令，未执行后续工具")
