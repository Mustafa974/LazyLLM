from __future__ import annotations
import copy
from typing import Any, Dict, List

from ..data_models.docir import DocBlock, DocIR
from ..data_models.revision import (
    BLOCK_META_FIELDS, HEADING_PATH_KEY, SECTION_META_FIELDS,
)
from ..data_models.writing import (
    DraftBlock, DraftDocument, DraftSection,
    OutlineNode, OutlineNodeConstraints, WritingOutline,
)


def outline_to_docir_blocks(outline: WritingOutline) -> List[DocBlock]:
    '''Convert a WritingOutline into a nested DocBlock list (no persistence).'''
    nodes = outline.nodes if hasattr(outline, 'nodes') else outline.get('nodes', [])

    def _convert(node_data, level):
        if isinstance(node_data, dict):
            node = OutlineNode(**node_data)
        else:
            node = node_data
        meta = copy.deepcopy(node.meta)
        if node.instruction:
            meta['instruction'] = node.instruction
        if node.constraints:
            meta['constraints'] = node.constraints.model_dump(exclude_defaults=False)
        return DocBlock(
            block_id=node.node_id or '',
            block_type='heading',
            text=node.title,
            level=level,
            children=[_convert(c, level + 1) for c in node.children],
            meta=meta,
        )

    return [_convert(n, 1) for n in nodes]


def docir_blocks_to_outline_nodes(blocks: List[DocBlock]) -> List[OutlineNode]:
    '''Convert a nested DocBlock list into OutlineNode list (no persistence).'''

    def _convert(block):
        if block.block_type != 'heading':
            return None
        meta = block.meta or {}
        constraints_dict = meta.get('constraints', {})
        constraints = (
            OutlineNodeConstraints(**constraints_dict) if constraints_dict
            else OutlineNodeConstraints()
        )
        children = [c for c in (_convert(c) for c in block.children) if c is not None]
        return OutlineNode(
            node_id=block.block_id,
            title=block.text,
            level=block.level or 1,
            instruction=meta.get('instruction'),
            constraints=constraints,
            children=children,
            meta={k: v for k, v in meta.items()
                  if k not in ('instruction', 'constraints')},
        )

    return [c for c in (_convert(b) for b in blocks) if c is not None]


def docir_to_outline(source: DocIR) -> WritingOutline:
    '''Convert a DocIR into a WritingOutline (no persistence).'''
    if (source.meta or {}).get('source_kind') != 'outline':
        raise ValueError(
            f'Expected source_kind="outline" in DocIR meta, '
            f'got {source.meta.get("source_kind")!r}. '
            f'Use only for DocIR artifacts produced by outline_to_doc_ir.'
        )
    nodes = docir_blocks_to_outline_nodes(source.blocks)
    return WritingOutline(
        outline_id=source.doc_id,
        title=source.title,
        nodes=nodes,
    )


def draft_to_docir_blocks(draft: DraftDocument) -> List[DocBlock]:
    '''Convert a DraftDocument into a flat DocBlock list (no persistence).'''
    blocks: List[DocBlock] = []

    def _flatten(sections, depth, heading_path):
        for section in sections:
            current_path = list(heading_path)
            if section.title:
                current_path.append(section.title)
            # heading block
            bid = f'{section.section_id}::heading' if section.section_id else f'block-{len(blocks) + 1}'
            meta: Dict[str, Any] = {HEADING_PATH_KEY: list(current_path)}
            for field in SECTION_META_FIELDS:
                val = getattr(section, field, None)
                if val:
                    meta[field] = val
            blocks.append(DocBlock(
                block_id=bid, block_type='heading',
                text=section.title or '', level=depth, meta=meta,
            ))
            # paragraph blocks
            for idx, blk in enumerate(section.blocks, start=1):
                if blk.block_id:
                    pbid = blk.block_id
                elif section.section_id:
                    pbid = f'{section.section_id}::block-{idx}'
                else:
                    pbid = f'block-{len(blocks) + 1}'
                pmeta: Dict[str, Any] = {HEADING_PATH_KEY: list(current_path)}
                for field in BLOCK_META_FIELDS:
                    val = getattr(blk, field, None)
                    if val:
                        pmeta[field] = val
                blocks.append(DocBlock(
                    block_id=pbid, block_type='paragraph',
                    text=blk.content, meta=pmeta,
                ))
            _flatten(section.sub_sections, depth + 1, current_path)

    _flatten(draft.sections if hasattr(draft, 'sections')
             else draft.get('sections', []), 1, [])
    return blocks


def docir_to_draft(source: DocIR) -> DraftDocument:
    '''Convert a DocIR into a DraftDocument (no persistence).'''
    if (source.meta or {}).get('source_kind') != 'draft_document':
        raise ValueError(
            f'Expected source_kind="draft_document" in DocIR meta, '
            f'got {source.meta.get("source_kind")!r}. '
            f'Use only for DocIR artifacts produced from DraftDocument.'
        )
    root = DraftSection()
    stack = [(0, root)]
    for block in source.blocks:
        if block.block_type == 'heading':
            level = block.level if block.level is not None else 1
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            meta = block.meta or {}
            section = DraftSection(
                **{field: meta.get(field) for field in SECTION_META_FIELDS},
                title=block.text or None,
            )
            stack[-1][1].sub_sections.append(section)
            stack.append((level, section))
        else:
            parent = stack[-1][1]
            meta = block.meta or {}
            parent.blocks.append(DraftBlock(
                block_id=block.block_id,
                section_id=parent.section_id,
                **{field: meta.get(field) for field in BLOCK_META_FIELDS},
                content=block.text,
            ))
    sections = list(root.sub_sections)
    if root.blocks:
        sections.insert(0, DraftSection(blocks=root.blocks))
    return DraftDocument(
        draft_id=source.doc_id,
        title=source.title,
        sections=sections,
    )
