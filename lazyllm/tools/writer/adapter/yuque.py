from __future__ import annotations

import re
from uuid import uuid4

from ..numbering import (
    MARKDOWN_ANCHOR_RE,
    build_numbering_view_from_markdown,
    compute_numbering,
    find_markdown_images,
    materialize_markdown,
    strip_markdown_heading_numbering_config,
)


_FENCE_RE = re.compile(r'^[ \t]*(`{3,}|~{3,})[^\r\n]*')
_INLINE_LITERAL_RE = re.compile(r'\\[^\r\n]|(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)')
_INTERNAL_REFERENCE_RE = re.compile(
    r'(?<!!)\[(?:[^\[\]]|\[[^\[\]]*\])*\]\([ \t]*#[^\s)]*(?:[ \t]+["\'][^\r\n]*?["\'])?[ \t]*\)',
)
_SPAN_ANCHOR_RE = re.compile(r'<span\s+id=["\']block-[^"\']+["\']\s*>\s*</span\s*>', re.IGNORECASE)


class YuqueMarkdownAdapter:
    provider = 'yuque'

    def convert(self, markdown: str) -> str:
        if not isinstance(markdown, str):
            raise TypeError(f'markdown must be a string, got {type(markdown).__name__}.')

        # Convert a copy of the Writer source, keeping code examples opaque to
        # the numbering and image scanners. This method performs no file I/O.
        protected, literals = self._protect_literals(markdown)
        protected = _INTERNAL_REFERENCE_RE.sub('', protected)
        view = build_numbering_view_from_markdown(protected)
        converted = materialize_markdown(protected, view, compute_numbering(view))
        converted = '\n'.join(self._remove_images(line) for line in converted.split('\n'))
        # Heading anchors carry numbering rules, so remove them only after rendering.
        converted = self._remove_internal_references(converted)
        converted = strip_markdown_heading_numbering_config(converted)
        if markdown.endswith('\n'):
            converted += '\n'
        for token, source in reversed(literals):
            converted = converted.replace(token, source)
        return converted

    @staticmethod
    def _remove_internal_references(markdown: str) -> str:
        markdown = _INTERNAL_REFERENCE_RE.sub('', markdown)
        markdown = MARKDOWN_ANCHOR_RE.sub('', markdown)
        return _SPAN_ANCHOR_RE.sub('', markdown)

    @staticmethod
    def _protect_literals(markdown: str) -> tuple[str, list[tuple[str, str]]]:
        prefix = f'YUQUE_LITERAL_{uuid4().hex}_'
        literals: list[tuple[str, str]] = []

        def reserve(source: str) -> str:
            token = f'{prefix}{len(literals)}'
            literals.append((token, source))
            return token

        lines = markdown.splitlines(keepends=True)
        output: list[str] = []
        index = 0
        while index < len(lines):
            opening = _FENCE_RE.match(lines[index])
            if opening is None:
                output.append(lines[index])
                index += 1
                continue
            fence = opening.group(1)
            closing = re.compile(rf'^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*(?:\r?\n)?$')
            end = index + 1
            while end < len(lines) and not closing.fullmatch(lines[end]):
                end += 1
            end = min(end + 1, len(lines))
            source = ''.join(lines[index:end])
            # Keep a code target for numbering, but hide nested fences and images.
            token = reserve(source.removesuffix('\n'))
            wrapper = f'```text\n{token}\n```'
            literals[-1] = (wrapper, literals[-1][1])
            output.append(wrapper + ('\n' if source.endswith('\n') else ''))
            index = end

        # Apply inline protection only outside the fenced placeholders.
        protected = ''.join(output)
        parts = re.split(r'(```text\n' + re.escape(prefix) + r'\d+\n```)', protected)
        for index in range(0, len(parts), 2):
            parts[index] = _INLINE_LITERAL_RE.sub(lambda match: reserve(match.group(0)), parts[index])
        return ''.join(parts), literals

    @staticmethod
    def _destination_end(line: str, start: int) -> int | None:
        depth = 0
        quote = ''
        for index in range(start, len(line)):
            char = line[index]
            if quote:
                if char == quote:
                    quote = ''
            elif char == '<':
                quote = '>'
            elif char in {'"', "'"} and index > start and line[index - 1].isspace():
                quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return index + 1
        return None

    @classmethod
    def _remove_images(cls, line: str) -> str:
        output: list[str] = []
        cursor = 0
        for image in find_markdown_images(line):
            start, end = image.start, image.end
            if start < cursor:
                continue
            if image.syntax == 'markdown':
                destination = line.find('](', start, end) + 1
                end = cls._destination_end(line, destination) or end
                # Remove a link containing only the image, without touching
                # ordinary links or the surrounding table/list structure.
                if start > cursor and line[start - 1] == '[' and line[end:end + 2] == '](':
                    link_end = cls._destination_end(line, end + 1)
                    if link_end is not None:
                        start, end = start - 1, link_end
            output.append(line[cursor:start])
            cursor = end
        output.append(line[cursor:])
        return ''.join(output)
