from __future__ import annotations
import copy
from typing import List

from ..data_models.docir import DocBlock, DocIR
from ..data_models.writing import OutlineNode, OutlineNodeConstraints, WritingOutline


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
