from __future__ import annotations

import html
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .writer_ir import WriterBlock, WriterDocument, WriterSpan


MarkdownAstNode = Dict[str, Any]

_DOCUMENT_COMMENT_RE = re.compile(
    r'^<!--\s*writer:document\s+id="([^"]+)"'
    r'(?:\s+node-id="([^"]+)")?\s+title="([^"]*)"\s*-->$'
)
_BLOCK_COMMENT_RE = re.compile(r'^<!--\s*id="([^"]+)"\s*-->$')
_MARKDOWN_ESCAPE_RE = re.compile(r'([\\`*_\[\]~|<])')
_ORDERED_LIST_PREFIX_RE = re.compile(r'^(\s*\d+)([.)])(?=\s)')
_SUPPORTED_BLOCK_TYPES = {'paragraph', 'heading', 'list_item', 'code', 'quote'}
_STYLE_ORDER = ('inline_code', 'strikethrough', 'italic', 'bold')


def _markdown_parser():
    try:
        import mistune
    except ImportError:
        raise ImportError('Markdown projection requires `pip install mistune>=3.0.0`') from None
    return mistune.create_markdown(renderer='ast', plugins=['strikethrough', 'table'])


def _render_ast(tokens: Iterable[MarkdownAstNode]) -> str:
    try:
        from mistune.core import BlockState
        from mistune.renderers.markdown import MarkdownRenderer
    except ImportError:
        raise ImportError('Markdown projection requires `pip install mistune>=3.0.0`') from None
    return MarkdownRenderer().render_tokens(tokens, BlockState()).strip()


@dataclass
class MarkdownBlockProjection:
    node_id: str
    ast_node: MarkdownAstNode

    def __str__(self) -> str:
        token = deepcopy(self.ast_node)
        if token['type'] == 'list_item':
            ordered = bool(token.pop('_ordered', False))
            token = {
                'type': 'list',
                'children': [token],
                'tight': True,
                'bullet': '.' if ordered else '-',
                'attrs': {'depth': 0, 'ordered': ordered},
            }
            return _render_ast([token])
        return f'<!-- id="{html.escape(self.node_id, quote=True)}" -->\n{_render_ast([token])}'


@dataclass
class MarkdownDocumentProjection:
    document_id: str
    title: str
    blocks: List[MarkdownBlockProjection]
    root_node_id: Optional[str] = None
    _source: str = field(default='', repr=False)

    @classmethod
    def from_writer_document(cls, document: WriterDocument) -> 'MarkdownDocumentProjection':
        root_node_id = None
        blocks = document.blocks
        if len(blocks) == 1 and blocks[0].type == 'document':
            root_node_id = blocks[0].node_id
            blocks = blocks[0].children
        source = cls._render_document(
            document.document_id, document.title, root_node_id, blocks,
        )
        return cls.parse(source)

    def to_writer_document(self) -> WriterDocument:
        blocks = [self._block_to_writer(block) for block in self.blocks]
        if self.root_node_id:
            blocks = [WriterBlock(
                node_id=self.root_node_id,
                type='document',
                content=self.title,
                spans=[WriterSpan(text=self.title)] if self.title else [],
                children=blocks,
                stage='final',
            )]
        self._validate_unique_ids(blocks)
        return WriterDocument(
            document_id=self.document_id,
            stage='final',
            title=self.title,
            blocks=blocks,
        )

    @classmethod
    def parse(cls, text: str) -> 'MarkdownDocumentProjection':
        tokens = _markdown_parser()(text)
        if not tokens or tokens[0].get('type') != 'block_html':
            raise ValueError('Markdown projection is missing document comment')
        document_match = _DOCUMENT_COMMENT_RE.fullmatch(tokens[0].get('raw', '').strip())
        if not document_match:
            raise ValueError('Markdown projection has invalid document comment')

        document_id, root_node_id, title = (
            html.unescape(value) if value is not None else None
            for value in document_match.groups()
        )
        blocks = cls._parse_blocks(tokens[1:])
        projection = cls(
            document_id=document_id,
            title=title or '',
            blocks=blocks,
            root_node_id=root_node_id,
            _source=text.strip() + '\n',
        )
        projection.to_writer_document()
        return projection

    def __str__(self) -> str:
        return self._source

    @classmethod
    def _render_document(
        cls, document_id: str, title: str, root_node_id: Optional[str],
        blocks: Iterable[WriterBlock],
    ) -> str:
        attributes = [f'id="{cls._escape_comment_value(document_id)}"']
        if root_node_id:
            attributes.append(f'node-id="{cls._escape_comment_value(root_node_id)}"')
        attributes.append(f'title="{cls._escape_comment_value(title)}"')
        header = f'<!-- writer:document {" ".join(attributes)} -->'
        body = cls._render_blocks(list(blocks), depth=0)
        return f'{header}\n\n{body}\n' if body else f'{header}\n'

    @classmethod
    def _render_blocks(cls, blocks: List[WriterBlock], depth: int) -> str:
        rendered: List[str] = []
        previous: Optional[WriterBlock] = None
        for block in blocks:
            fragment = cls._render_block(block, depth)
            if previous is None:
                rendered.append(fragment)
            elif (
                previous.type == block.type == 'list_item'
                and cls._is_ordered_list_item(previous) == cls._is_ordered_list_item(block)
            ):
                rendered.append('\n' + fragment)
            else:
                rendered.append('\n\n' + fragment)
            previous = block
        return ''.join(rendered)

    @classmethod
    def _render_block(cls, block: WriterBlock, depth: int) -> str:
        content = cls._render_spans(block)
        escaped_id = cls._escape_comment_value(block.node_id)
        indent = '    ' * depth

        if block.type == 'document':
            return cls._render_blocks(block.children, depth)
        if block.type == 'list_item':
            marker = '1.' if cls._is_ordered_list_item(block) else '-'
            line = f'{indent}{marker} {content}<!-- id="{escaped_id}" -->'
            if block.children:
                line += '\n' + cls._render_blocks(block.children, depth + 1)
            return line

        if block.type not in _SUPPORTED_BLOCK_TYPES:
            block_type = 'paragraph'
        else:
            block_type = block.type

        anchor = f'{indent}<!-- id="{escaped_id}" -->'
        if block_type == 'heading':
            level = int(block.numbering.get('level', 1))
            body = f'{"#" * level} {content}' if 1 <= level <= 6 else content
        elif block_type == 'code':
            fence = cls._code_fence(block.content)
            suffix = '' if block.content.endswith('\n') else '\n'
            body = f'{fence}\n{block.content}{suffix}{fence}'
        elif block_type == 'quote':
            body = '\n'.join(f'> {line}' for line in content.splitlines() or [''])
        else:
            body = content

        fragment = anchor + '\n' + cls._indent_text(body, indent)
        if block.children:
            fragment += '\n\n' + cls._render_blocks(block.children, depth)
        return fragment

    @classmethod
    def _render_spans(cls, block: WriterBlock) -> str:
        spans = block.spans or [WriterSpan(text=block.content)]
        return ''.join(cls._render_span(span) for span in spans)

    @classmethod
    def _render_span(cls, span: WriterSpan) -> str:
        styles = [style for style in _STYLE_ORDER if style in span.style]
        leading = span.text[:len(span.text) - len(span.text.lstrip())]
        trailing = span.text[len(span.text.rstrip()):]
        text = span.text[len(leading):len(span.text) - len(trailing) if trailing else None]
        if not text:
            return cls._escape_text(span.text)
        if 'inline_code' in styles:
            rendered = cls._render_code_span(text)
            styles.remove('inline_code')
        else:
            rendered = cls._escape_text(text)
        for style in styles:
            if style == 'strikethrough':
                rendered = f'~~{rendered}~~'
            elif style == 'italic':
                rendered = f'_{rendered}_'
            elif style == 'bold':
                rendered = f'**{rendered}**'
        return cls._escape_text(leading) + rendered + cls._escape_text(trailing)

    @staticmethod
    def _escape_text(text: str) -> str:
        lines = []
        for line in text.split('\n'):
            escaped = _MARKDOWN_ESCAPE_RE.sub(r'\\\1', line)
            content = escaped.lstrip()
            prefix_length = len(escaped) - len(content)
            if content.startswith(('#', '+', '-', '>')):
                escaped = escaped[:prefix_length] + '\\' + escaped[prefix_length:]
            else:
                escaped = _ORDERED_LIST_PREFIX_RE.sub(r'\1\\\2', escaped, count=1)
            lines.append(escaped)
        return '  \n'.join(lines)

    @staticmethod
    def _render_code_span(text: str) -> str:
        runs = [len(match.group(0)) for match in re.finditer(r'`+', text)]
        fence = '`' * max(1, (max(runs) + 1) if runs else 1)
        padding = ' ' if text.startswith('`') or text.endswith('`') else ''
        return f'{fence}{padding}{text}{padding}{fence}'

    @staticmethod
    def _code_fence(text: str) -> str:
        runs = [len(match.group(0)) for match in re.finditer(r'`{3,}', text)]
        return '`' * max(3, (max(runs) + 1) if runs else 3)

    @staticmethod
    def _indent_text(text: str, indent: str) -> str:
        if not indent:
            return text
        return '\n'.join(indent + line if line else line for line in text.splitlines())

    @staticmethod
    def _escape_comment_value(value: str) -> str:
        return html.escape(value, quote=True).replace('--', '&#45;&#45;')

    @staticmethod
    def _is_ordered_list_item(block: WriterBlock) -> bool:
        ordered = block.numbering.get('ordered')
        if isinstance(ordered, bool):
            return ordered
        return any(ref.get('source_block_type') == 13 for ref in block.source_refs)

    @classmethod
    def _parse_blocks(cls, tokens: Iterable[MarkdownAstNode]) -> List[MarkdownBlockProjection]:
        blocks: List[MarkdownBlockProjection] = []
        pending_id: Optional[str] = None
        for token in tokens:
            token_type = token.get('type')
            if token_type == 'block_html':
                match = _BLOCK_COMMENT_RE.fullmatch(token.get('raw', '').strip())
                if match:
                    if pending_id is not None:
                        raise ValueError(f'Markdown block id {pending_id!r} has no block')
                    pending_id = html.unescape(match.group(1))
                continue
            if token_type == 'list':
                if pending_id is not None:
                    raise ValueError(f'Markdown block id {pending_id!r} cannot target a list container')
                blocks.extend(cls._parse_list(token))
                continue
            if token_type not in {'paragraph', 'heading', 'block_quote', 'block_code'}:
                continue
            if pending_id is None:
                raise ValueError(f'Markdown {token_type} block is missing id comment')
            blocks.append(MarkdownBlockProjection(node_id=pending_id, ast_node=token))
            pending_id = None
        if pending_id is not None:
            raise ValueError(f'Markdown block id {pending_id!r} has no block')
        return blocks

    @classmethod
    def _parse_list(cls, token: MarkdownAstNode) -> List[MarkdownBlockProjection]:
        ordered = bool(token.get('attrs', {}).get('ordered'))
        blocks: List[MarkdownBlockProjection] = []
        for item in token.get('children', []):
            if item.get('type') != 'list_item':
                continue
            node_id = cls._list_item_id(item)
            if not node_id:
                raise ValueError('Markdown list item is missing id comment')
            item = deepcopy(item)
            item['_ordered'] = ordered
            blocks.append(MarkdownBlockProjection(node_id=node_id, ast_node=item))
        return blocks

    @staticmethod
    def _list_item_id(item: MarkdownAstNode) -> Optional[str]:
        for child in item.get('children', []):
            if child.get('type') not in {'block_text', 'paragraph'}:
                continue
            for inline in child.get('children', []):
                if inline.get('type') != 'inline_html':
                    continue
                match = _BLOCK_COMMENT_RE.fullmatch(inline.get('raw', '').strip())
                if match:
                    return html.unescape(match.group(1))
        return None

    @classmethod
    def _block_to_writer(cls, block: MarkdownBlockProjection) -> WriterBlock:
        token = block.ast_node
        token_type = token['type']
        numbering: Dict[str, object] = {}
        children: List[WriterBlock] = []

        if token_type == 'heading':
            block_type = 'heading'
            numbering['level'] = int(token.get('attrs', {}).get('level', 1))
            spans = cls._inline_spans(token.get('children', []))
            content = ''.join(span.text for span in spans)
        elif token_type == 'block_code':
            block_type = 'code'
            content = token.get('raw', '')
            if content.endswith('\n'):
                content = content[:-1]
            spans = []
        elif token_type == 'block_quote':
            block_type = 'quote'
            spans = cls._container_spans(token)
            content = ''.join(span.text for span in spans)
        elif token_type == 'list_item':
            block_type = 'list_item'
            numbering['ordered'] = bool(token.get('_ordered'))
            spans = cls._container_spans(token)
            content = ''.join(span.text for span in spans)
            for child in token.get('children', []):
                if child.get('type') != 'list':
                    continue
                children.extend(
                    cls._block_to_writer(nested) for nested in cls._parse_list(child)
                )
        else:
            block_type = 'paragraph'
            spans = cls._inline_spans(token.get('children', []))
            content = ''.join(span.text for span in spans)

        return WriterBlock(
            node_id=block.node_id,
            type=block_type,
            content=content,
            spans=spans,
            children=children,
            stage='final',
            numbering=numbering,
        )

    @classmethod
    def _container_spans(cls, token: MarkdownAstNode) -> List[WriterSpan]:
        for child in token.get('children', []):
            if child.get('type') in {'block_text', 'paragraph'}:
                return cls._inline_spans(child.get('children', []))
        return []

    @classmethod
    def _inline_spans(cls, tokens: Iterable[MarkdownAstNode]) -> List[WriterSpan]:
        parts: List[Tuple[str, Tuple[str, ...]]] = []

        def append(text: str, styles: Tuple[str, ...]) -> None:
            if not text:
                return
            if parts and parts[-1][1] == styles:
                parts[-1] = (parts[-1][0] + text, styles)
            else:
                parts.append((text, styles))

        def walk(items: Iterable[MarkdownAstNode], styles: Tuple[str, ...]) -> None:
            for item in items:
                token_type = item.get('type')
                if token_type == 'text':
                    append(item.get('raw', ''), styles)
                elif token_type in {'softbreak', 'linebreak'}:
                    append('\n', styles)
                elif token_type == 'codespan':
                    append(item.get('raw', ''), styles + ('inline_code',))
                elif token_type == 'strong':
                    walk(item.get('children', []), styles + ('bold',))
                elif token_type == 'emphasis':
                    walk(item.get('children', []), styles + ('italic',))
                elif token_type == 'strikethrough':
                    walk(item.get('children', []), styles + ('strikethrough',))
                elif token_type == 'link':
                    walk(item.get('children', []), styles + ('link',))

        walk(tokens, ())
        return [WriterSpan(text=text, style=list(styles)) for text, styles in parts]

    @staticmethod
    def _validate_unique_ids(blocks: Iterable[WriterBlock]) -> None:
        seen = set()

        def walk(items: Iterable[WriterBlock]) -> None:
            for block in items:
                if block.node_id in seen:
                    raise ValueError(f'duplicate Markdown block id: {block.node_id!r}')
                seen.add(block.node_id)
                walk(block.children)

        walk(blocks)


__all__ = ['MarkdownBlockProjection', 'MarkdownDocumentProjection']
