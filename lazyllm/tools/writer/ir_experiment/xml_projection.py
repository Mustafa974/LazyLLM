from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .writer_ir import WriterBlock, WriterDocument, WriterSpan


_BLOCK_TAG_TO_TYPE = {
    'p': 'paragraph',
    'li': 'list_item',
    'pre': 'code',
    'blockquote': 'quote',
    'table': 'table',
    'td': 'table_cell',
    **{f'h{level}': 'heading' for level in range(1, 10)},
}
_TYPE_TO_BLOCK_TAG = {
    'paragraph': 'p',
    'list_item': 'li',
    'code': 'pre',
    'quote': 'blockquote',
    'table': 'table',
    'table_cell': 'td',
}
_CONTAINER_TAGS = {'document', 'ul', 'ol', 'thead', 'tbody', 'tr'}
_STYLE_TO_TAG = {
    'link': 'a',
    'bold': 'b',
    'italic': 'em',
    'strikethrough': 'del',
    'underline': 'u',
    'inline_code': 'code',
}
_TAG_TO_STYLE = {tag: style for style, tag in _STYLE_TO_TAG.items()}
_STYLE_ORDER = tuple(_STYLE_TO_TAG)


@dataclass
class XmlBlockProjection:
    element: ET.Element

    @property
    def node_id(self) -> str:
        node_id = self.element.get('id')
        if not node_id:
            raise ValueError(f'XML block <{self.element.tag}> is missing id')
        return node_id

    def __str__(self) -> str:
        return ET.tostring(self.element, encoding='unicode', short_empty_elements=True)


@dataclass
class XmlDocumentProjection:
    root: ET.Element

    @classmethod
    def from_writer_document(cls, document: WriterDocument) -> 'XmlDocumentProjection':
        root = ET.Element('document', {'id': document.document_id})
        title = ET.SubElement(root, 'title')
        title.text = document.title

        blocks = document.blocks
        if len(blocks) == 1 and blocks[0].type == 'document':
            root.set('node-id', blocks[0].node_id)
            blocks = blocks[0].children
        cls._append_blocks(root, blocks)
        return cls(root=root)

    def to_writer_document(self) -> WriterDocument:
        document_id = self.root.get('id')
        if not document_id:
            raise ValueError('XML document is missing id')
        title_element = self.root.find('title')
        title = ''.join(title_element.itertext()) if title_element is not None else ''
        blocks = self._parse_child_blocks(self.root)
        root_node_id = self.root.get('node-id')
        if root_node_id:
            blocks = [WriterBlock(
                node_id=root_node_id,
                type='document',
                content=title,
                spans=[WriterSpan(text=title)] if title else [],
                children=blocks,
                stage='final',
            )]
        self._validate_unique_ids(blocks)
        return WriterDocument(
            document_id=document_id,
            stage='final',
            title=title,
            blocks=blocks,
        )

    @classmethod
    def parse(cls, text: str) -> 'XmlDocumentProjection':
        root = ET.fromstring(text)
        if root.tag != 'document':
            raise ValueError(f'expected <document> root, got <{root.tag}>')
        return cls(root=root)

    def __str__(self) -> str:
        return ET.tostring(self.root, encoding='unicode', short_empty_elements=True)

    @classmethod
    def _append_blocks(cls, parent: ET.Element, blocks: Iterable[WriterBlock]) -> None:
        blocks = list(blocks)
        index = 0
        while index < len(blocks):
            block = blocks[index]
            if block.type != 'list_item':
                parent.append(cls._block_to_element(block))
                index += 1
                continue

            ordered = cls._is_ordered_list_item(block)
            container = ET.SubElement(parent, 'ol' if ordered else 'ul')
            while (
                index < len(blocks)
                and blocks[index].type == 'list_item'
                and cls._is_ordered_list_item(blocks[index]) == ordered
            ):
                container.append(cls._block_to_element(blocks[index]))
                index += 1

    @classmethod
    def _block_to_element(cls, block: WriterBlock) -> ET.Element:
        if block.type == 'heading':
            level = int(block.numbering.get('level', 1))
            if level < 1 or level > 9:
                raise ValueError(f'heading {block.node_id!r} has invalid level {level}')
            tag = f'h{level}'
        else:
            tag = _TYPE_TO_BLOCK_TAG.get(block.type)
            if tag is None:
                raise ValueError(f'unsupported XML block type: {block.type!r}')

        element = ET.Element(tag, {'id': block.node_id})
        if block.type == 'code':
            code = ET.SubElement(element, 'code')
            code.text = block.content
        else:
            cls._append_inline_content(element, block)
        cls._append_blocks(element, block.children)
        return element

    @staticmethod
    def _is_ordered_list_item(block: WriterBlock) -> bool:
        ordered = block.numbering.get('ordered')
        if isinstance(ordered, bool):
            return ordered
        return any(ref.get('source_block_type') == 13 for ref in block.source_refs)

    @classmethod
    def _append_inline_content(cls, element: ET.Element, block: WriterBlock) -> None:
        spans = block.spans or [WriterSpan(text=block.content)]
        for span in spans:
            styles = [style for style in _STYLE_ORDER if style in span.style]
            if not styles:
                cls._append_text(element, span.text)
                continue
            outer = ET.Element(_STYLE_TO_TAG[styles[0]])
            current = outer
            for style in styles[1:]:
                child = ET.SubElement(current, _STYLE_TO_TAG[style])
                current = child
            current.text = span.text
            element.append(outer)

    @staticmethod
    def _append_text(element: ET.Element, text: str) -> None:
        if not len(element):
            element.text = (element.text or '') + text
        else:
            last = element[-1]
            last.tail = (last.tail or '') + text

    @classmethod
    def _parse_child_blocks(cls, parent: ET.Element) -> List[WriterBlock]:
        blocks: List[WriterBlock] = []
        for child in parent:
            if child.tag == 'title' or child.tag in _TAG_TO_STYLE or child.tag == 'code':
                continue
            if child.tag in _BLOCK_TAG_TO_TYPE and child.get('id'):
                blocks.append(cls._element_to_block(child, parent.tag))
            elif child.tag in _CONTAINER_TAGS:
                blocks.extend(cls._parse_child_blocks(child))
        return blocks

    @classmethod
    def _element_to_block(cls, element: ET.Element, parent_tag: str) -> WriterBlock:
        projection = XmlBlockProjection(element)
        block_type = _BLOCK_TAG_TO_TYPE[element.tag]
        numbering: Dict[str, object] = {}
        if block_type == 'heading':
            numbering['level'] = int(element.tag[1:])
        elif block_type == 'list_item':
            numbering['ordered'] = parent_tag == 'ol'

        if block_type == 'code':
            code = element.find('code')
            content = ''.join(code.itertext()) if code is not None else ''
            spans: List[WriterSpan] = []
        else:
            spans = cls._extract_inline_spans(element)
            content = ''.join(span.text for span in spans)

        return WriterBlock(
            node_id=projection.node_id,
            type=block_type,
            content=content,
            spans=spans,
            children=cls._parse_child_blocks(element),
            stage='final',
            numbering=numbering,
        )

    @classmethod
    def _extract_inline_spans(cls, element: ET.Element) -> List[WriterSpan]:
        parts: List[Tuple[str, Tuple[str, ...]]] = []

        def append(text: Optional[str], styles: Tuple[str, ...]) -> None:
            if not text:
                return
            if parts and parts[-1][1] == styles:
                parts[-1] = (parts[-1][0] + text, styles)
            else:
                parts.append((text, styles))

        def walk(node: ET.Element, styles: Tuple[str, ...]) -> None:
            append(node.text, styles)
            for child in node:
                style = _TAG_TO_STYLE.get(child.tag)
                if style:
                    walk(child, styles + (style,))
                    append(child.tail, styles)
                elif child.tag == 'br':
                    append('\n', styles)
                    append(child.tail, styles)
                else:
                    if child.tail and not child.tail.isspace():
                        append(child.tail, styles)

        walk(element, ())
        return [WriterSpan(text=text, style=list(styles)) for text, styles in parts]

    @staticmethod
    def _validate_unique_ids(blocks: Iterable[WriterBlock]) -> None:
        seen = set()

        def walk(items: Iterable[WriterBlock]) -> None:
            for block in items:
                if block.node_id in seen:
                    raise ValueError(f'duplicate XML block id: {block.node_id!r}')
                seen.add(block.node_id)
                walk(block.children)

        walk(blocks)


__all__ = ['XmlBlockProjection', 'XmlDocumentProjection']
