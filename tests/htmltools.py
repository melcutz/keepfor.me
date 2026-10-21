# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""HTML helpers for the tests: parse the markup, never regex it.

A regular expression cannot match every tag a browser accepts (think
``</script foo="bar">``), so CodeQL's ``py/bad-tag-filter`` flags the
ones that try. These helpers use the stdlib parser instead.
"""

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class Script:
    """One ``<script>`` element: its attributes, its body, its source span."""

    attrs: dict[str, str | None]
    start: int
    end: int = 0
    body: str = ""


class _ScriptParser(HTMLParser):
    """Collects the ``<script>`` elements of a document."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self._line_starts = [0]
        for line in source.splitlines(keepends=True):
            self._line_starts.append(self._line_starts[-1] + len(line))
        self.scripts: list[Script] = []
        self._open: Script | None = None
        self._body: list[str] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "script":
            self._open = Script(dict(attrs), self._offset())
            self._body = []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._body.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._open is not None:
            self._open.body = "".join(self._body)
            self._open.end = self.source.index(">", self._offset()) + 1
            self.scripts.append(self._open)
            self._open = None


def scripts(source: str) -> list[Script]:
    """Every ``<script>`` element of the document."""
    parser = _ScriptParser(source)
    parser.feed(source)
    parser.close()
    return parser.scripts


def script_bodies(source: str) -> str:
    """The bodies of every ``<script>`` element, joined into one blob."""
    return "\n".join(s.body for s in scripts(source))


def without_scripts(source: str, script_type: str | None = None) -> str:
    """The document without its ``<script>`` elements (of one type, if given)."""
    doomed = [
        s
        for s in scripts(source)
        if script_type is None or s.attrs.get("type") == script_type
    ]
    for s in reversed(doomed):
        source = source[: s.start] + source[s.end :]
    return source
