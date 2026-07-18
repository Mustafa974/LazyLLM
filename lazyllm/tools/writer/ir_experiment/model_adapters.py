from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..utils import to_prompt_json
from .markdown_projection import MarkdownDocumentProjection
from .writer_ir import PatchSet, WriterDocument
from .xml_projection import XmlDocumentProjection


def _validate_patch(
    model_output: Any, document: WriterDocument, representation: str,
) -> PatchSet:
    patch = model_output if isinstance(model_output, PatchSet) else PatchSet.model_validate(model_output)
    if patch.target_doc_id != document.document_id:
        raise ValueError(
            f'patch targets {patch.target_doc_id!r}, not {document.document_id!r}'
        )
    node_map = {}
    pending_blocks = list(document.blocks)
    while pending_blocks:
        block = pending_blocks.pop()
        node_map[block.node_id] = block
        pending_blocks.extend(block.children)
    for hunk in patch.hunks:
        if hunk.old_text is not None:
            raise ValueError('LLM-generated PatchHunk must omit old_text')
        target = node_map.get(hunk.target_node_id)
        if target is None:
            raise ValueError(
                f'patch targets node_id absent from WriterDocument: {hunk.target_node_id!r}'
            )
        if hunk.modify_type == 'replace':
            start, end = hunk.text_range
            if start < 0 or end < start or end > len(target.content):
                raise ValueError(
                    f'invalid text_range {hunk.text_range!r} for node {target.node_id!r}'
                )
            hunk.old_text = target.content[start:end]
        elif hunk.modify_type == 'delete':
            hunk.old_text = target.content
        if hunk.anchor_node_id is not None and hunk.anchor_node_id not in node_map:
            raise ValueError(
                f'patch anchor absent from WriterDocument: {hunk.anchor_node_id!r}'
            )
    patch.meta['model_representation'] = representation
    return patch


@runtime_checkable
class ModelAdapter(Protocol):
    name: str

    def document_to_model_input(self, document: WriterDocument) -> str: ...

    def model_output_to_patch(self, model_output: Any, document: WriterDocument) -> PatchSet: ...


class StructuredJsonAdapter:
    '''JSON 恒等投影。'''

    name = 'json'

    def document_to_model_input(self, document: WriterDocument) -> str:
        return to_prompt_json(document)

    def model_output_to_patch(self, model_output: Any, document: WriterDocument) -> PatchSet:
        return _validate_patch(model_output, document, self.name)


class XmlAdapter:
    '''XML 原生文档投影。'''

    name = 'xml'

    def document_to_model_input(self, document: WriterDocument) -> str:
        return str(XmlDocumentProjection.from_writer_document(document))

    def model_output_to_patch(self, model_output: Any, document: WriterDocument) -> PatchSet:
        return _validate_patch(model_output, document, self.name)


class MarkdownAdapter:
    '''带核心 Block ID 的原生 Markdown 投影。'''

    name = 'markdown'

    def document_to_model_input(self, document: WriterDocument) -> str:
        return str(MarkdownDocumentProjection.from_writer_document(document))

    def model_output_to_patch(self, model_output: Any, document: WriterDocument) -> PatchSet:
        return _validate_patch(model_output, document, self.name)


__all__ = ['ModelAdapter', 'StructuredJsonAdapter', 'XmlAdapter', 'MarkdownAdapter']
