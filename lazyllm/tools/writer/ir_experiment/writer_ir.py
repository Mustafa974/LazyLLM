from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator


WriterStage = Literal['outline', 'draft', 'final']


class WriterSpan(BaseModel):
    text: str = ''
    style: List[str] = Field(default_factory=list)


class WriterBlock(BaseModel):
    node_id: str
    type: str
    content: str = ''
    spans: List[WriterSpan] = Field(default_factory=list)
    children: List['WriterBlock'] = Field(default_factory=list)
    stage: WriterStage
    status: str = ''
    authoring: Dict[str, Any] = Field(default_factory=dict)
    numbering: Dict[str, Any] = Field(default_factory=dict)
    references: List[Dict[str, Any]] = Field(default_factory=list)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    provider_binding: Dict[str, Any] = Field(default_factory=dict)
    provider_payload: Dict[str, Any] = Field(default_factory=dict)
    editable: bool = True

    @model_validator(mode='after')
    def validate_spans(self) -> 'WriterBlock':
        if self.spans and ''.join(span.text for span in self.spans) != self.content:
            raise ValueError('content must equal the concatenated span text when spans are present')
        return self


WriterBlock.model_rebuild()


class WriterDocument(BaseModel):
    document_id: str
    stage: WriterStage
    title: str = ''
    blocks: List[WriterBlock] = Field(default_factory=list)
    revision: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    provider_binding: Dict[str, Any] = Field(default_factory=dict)


PatchModifyType = Literal['replace', 'insert', 'delete', 'move']
PatchPosition = Literal['before', 'after']
PatchBlockType = Literal['paragraph', 'heading', 'list_item', 'code', 'quote']


class ModifyInstruction(BaseModel):
    instruction_id: Optional[str] = None
    target_node_id: str
    modify_type: PatchModifyType
    anchor_node_id: Optional[str] = None
    position: Optional[PatchPosition] = None
    instruction: str

    @model_validator(mode='after')
    def validate_operation(self) -> 'ModifyInstruction':
        if self.modify_type in ('insert', 'move') and self.position is None:
            raise ValueError(f'{self.modify_type} requires position')
        if self.modify_type == 'move' and self.anchor_node_id is None:
            raise ValueError('move requires anchor_node_id')
        if self.modify_type != 'move' and self.anchor_node_id is not None:
            raise ValueError('anchor_node_id is only valid for move')
        if self.modify_type not in ('insert', 'move') and self.position is not None:
            raise ValueError('position is only valid for insert and move')
        return self


class ModifyPlan(BaseModel):
    plan_id: Optional[str] = None
    task_id: Optional[str] = None
    scope: Literal['document', 'section', 'block', 'span']
    target_node_ids: List[str] = Field(default_factory=list)
    instructions: List[ModifyInstruction] = Field(default_factory=list)
    summary: Optional[str] = None


class PatchBlock(BaseModel):
    type: PatchBlockType
    content: str = ''
    numbering: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_numbering(self) -> 'PatchBlock':
        if self.type == 'heading':
            level = self.numbering.get('level')
            if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 9:
                raise ValueError('heading requires an integer numbering.level from 1 to 9')
        elif self.type == 'list_item':
            if not isinstance(self.numbering.get('ordered'), bool):
                raise ValueError('list_item requires boolean numbering.ordered')
        elif self.numbering:
            raise ValueError(f'{self.type} does not use numbering')
        return self


class PatchHunk(BaseModel):
    target_node_id: str
    modify_type: PatchModifyType
    anchor_node_id: Optional[str] = None
    text_range: Optional[Tuple[int, int]] = None
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    new_blocks: List[PatchBlock] = Field(default_factory=list)
    position: Optional[PatchPosition] = None

    @model_validator(mode='after')
    def validate_operation(self) -> 'PatchHunk':
        if self.modify_type == 'replace':
            if self.text_range is None or self.new_text is None:
                raise ValueError('replace requires text_range and new_text')
            if self.anchor_node_id is not None or self.new_blocks or self.position is not None:
                raise ValueError('replace contains fields for another operation')
        elif self.modify_type == 'insert':
            if self.position is None or not self.new_blocks:
                raise ValueError('insert requires position and new_blocks')
            if (
                self.anchor_node_id is not None or self.text_range is not None
                or self.old_text is not None or self.new_text is not None
            ):
                raise ValueError('insert contains fields for another operation')
        elif self.modify_type == 'delete':
            if (
                self.anchor_node_id is not None or self.text_range is not None
                or self.new_text is not None or self.new_blocks or self.position is not None
            ):
                raise ValueError('delete contains fields for another operation')
        elif self.anchor_node_id is None or self.position is None:
            raise ValueError('move requires anchor_node_id and position')
        elif (
            self.text_range is not None or self.old_text is not None
            or self.new_text is not None or self.new_blocks
        ):
            raise ValueError('move contains fields for another operation')
        return self


class PatchSet(BaseModel):
    patch_id: Optional[str] = None
    target_doc_id: str
    hunks: List[PatchHunk] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class PatchResult(BaseModel):
    patch_id: Optional[str] = None
    success: bool
    applied_hunks: List[str] = Field(default_factory=list)
    failed_hunks: List[str] = Field(default_factory=list)
    message: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


WriterDocument.model_rebuild()


__all__ = [
    'WriterDocument', 'WriterBlock', 'WriterSpan', 'WriterStage',
    'ModifyInstruction', 'ModifyPlan',
    'PatchBlock', 'PatchHunk', 'PatchSet', 'PatchResult',
]
