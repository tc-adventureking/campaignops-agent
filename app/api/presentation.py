"""Local Markdown presentation with raw HTML and remote images disabled."""

from markdown_it import MarkdownIt
from pydantic import Field

from app.domain.models import Contract


class MarkdownRequest(Contract):
    markdown: str = Field(max_length=100000)


class MarkdownResponse(Contract):
    html: str


def compile_markdown(markdown: str) -> str:
    # js-default disables raw HTML and rejects unsafe link protocols. No CDN or plugins.
    parser = MarkdownIt("js-default", {"linkify": False}).disable("image")
    return str(parser.render(markdown))
