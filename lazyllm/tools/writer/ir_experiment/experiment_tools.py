from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple
from uuid import uuid4

from ..data_models.task import TargetDocument, WritingTask
from ..tools.base import WriterToolBase
from ..utils import to_prompt_json
from .model_adapters import MarkdownAdapter, StructuredJsonAdapter, XmlAdapter
from .writer_ir import (
    ModifyPlan, PatchBlock, PatchHunk, PatchResult, PatchSet, WriterBlock, WriterDocument, WriterSpan,
    WriterStage,
)

# 飞书 block_type 整数 -> WriterBlock.type 字符串。
# heading 统一归为 'heading'，level 写入 numbering；其余未知类型一律 'block'，保持开放。
_FEISHU_TYPE_MAP = {
    1: 'document', 2: 'paragraph',
    3: 'heading', 4: 'heading', 5: 'heading', 6: 'heading', 7: 'heading',
    8: 'heading', 9: 'heading', 10: 'heading', 11: 'heading',
    12: 'list_item', 13: 'list_item', 14: 'code', 15: 'quote',
    27: 'image', 31: 'table', 32: 'table_cell',
}

_FEISHU_TEXT_FIELDS = {
    1: 'page', 2: 'text',
    3: 'heading1', 4: 'heading2', 5: 'heading3', 6: 'heading4', 7: 'heading5',
    8: 'heading6', 9: 'heading7', 10: 'heading8', 11: 'heading9',
    12: 'bullet', 13: 'ordered', 14: 'code', 15: 'quote', 17: 'todo',
}

_WRITER_TYPE_TO_FEISHU = {
    'document': 1, 'paragraph': 2, 'list_item': 12,
    'code': 14, 'quote': 15,
}


class IRExperimentTools(WriterToolBase):
    '''内层 WriterDocument 与三种模型边界 Adapter 的实验骨架。'''

    __public_apis__ = [
        'read_writer_doc',
        'generate_modify_plan_json', 'generate_modify_plan_xml', 'generate_modify_plan_markdown',
        'generate_patch_json', 'generate_patch_xml', 'generate_patch_markdown',
        'apply_patch', 'write_patch_to_feishu',
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.json_adapter = StructuredJsonAdapter()
        self.xml_adapter = XmlAdapter()
        self.markdown_adapter = MarkdownAdapter()

    # ==================== 共用：飞书 -> 内层 WriterDocument ====================

    def read_writer_doc(self, feishu_url: str) -> WriterDocument:
        '''读取飞书文档并构造内层 WriterDocument（唯一 IR）。'''
        fs, real_path = self._resolve_feishu(feishu_url)
        resolved = fs.resolve_link(real_path)
        doc_id = resolved.get('obj_token')
        if not doc_id:
            raise ValueError(f'cannot resolve feishu url to a document: {feishu_url!r}')
        raw_blocks = fs._get_doc_blocks_raw(doc_id, with_descendants=True)
        blocks = self._build_writer_blocks(raw_blocks)
        stage: WriterStage = 'final'
        return WriterDocument(
            document_id=doc_id,
            stage=stage,
            title=resolved.get('title') or '',
            blocks=blocks,
            provider_binding={'adapter': 'feishu', 'uri': feishu_url},
            metadata={
                'title': resolved.get('title') or '',
                'source': TargetDocument(
                    doc_id=doc_id, uri=feishu_url, adapter='feishu',
                    title=resolved.get('title') or None,
                ).model_dump(),
                'block_count': sum(1 for _ in self._iter_blocks(blocks)),
            },
        )

    # ==================== Plan：三路线入口，差异仅在 Adapter ====================

    def generate_modify_plan_json(self, task: Any, document: Any) -> ModifyPlan:
        return self._generate_modify_plan(self.json_adapter, task, document)

    def generate_modify_plan_xml(self, task: Any, document: Any) -> ModifyPlan:
        return self._generate_modify_plan(self.xml_adapter, task, document)

    def generate_modify_plan_markdown(self, task: Any, document: Any) -> ModifyPlan:
        return self._generate_modify_plan(self.markdown_adapter, task, document)

    # ==================== Patch：三路线入口，差异仅在 Adapter ====================

    def generate_patch_json(self, modify_plan: Any, document: Any) -> PatchSet:
        return self._generate_patch(self.json_adapter, modify_plan, document)

    def generate_patch_xml(self, modify_plan: Any, document: Any) -> PatchSet:
        return self._generate_patch(self.xml_adapter, modify_plan, document)

    def generate_patch_markdown(self, modify_plan: Any, document: Any) -> PatchSet:
        return self._generate_patch(self.markdown_adapter, modify_plan, document)

    # ==================== 共用：Patch 应用到内层 WriterDocument ====================

    def apply_patch(self, document: Any, patch_set: Any) -> Tuple[WriterDocument, PatchResult]:
        '''按 target_node_id 把 PatchSet 应用到 WriterDocument 副本。'''
        source = self._unified_model(document, WriterDocument)
        patch = self._unified_model(patch_set, PatchSet)
        if patch.target_doc_id != source.document_id:
            raise ValueError(
                f'patch targets {patch.target_doc_id!r}, not {source.document_id!r}'
            )

        working = source.model_copy(deep=True)
        failed_hunk = self._apply_patch_atomically(working, patch)
        if failed_hunk is not None:
            return source.model_copy(deep=True), self._patch_result(
                patch, [], [failed_hunk], 'Patch validation or application failed.',
            )
        applied = [f'hunk-{index}' for index in range(len(patch.hunks))]
        return working, self._patch_result(patch, applied, [])

    # ==================== 共用：内层 Patch 写回飞书 ====================

    def write_patch_to_feishu(self, feishu_url: str, patch_set: Any) -> PatchResult:
        return self._write_patch_to_feishu(feishu_url, patch_set)

    # ==================== 私有：Plan / Patch 共用模型调用 ====================

    def _generate_modify_plan(
        self, adapter, task: Any, document: Any,
    ) -> ModifyPlan:
        doc = self._unified_model(document, WriterDocument)
        writing_task = self._unified_model(task, WritingTask)
        model_input = adapter.document_to_model_input(doc)
        prompt = f'''You are planning a local document revision using the {adapter.name} representation.
Understand the user request and return a ModifyPlan before any patch is generated.
Locate every node involved in the requested replace / insert / delete / move operation, including
destination anchors and nodes whose cross-references may be affected. Use only node_ids present in
the document. One instruction edits exactly one existing block. If a logical section consists of
multiple sibling blocks, emit one delete or move instruction for every block. For move, set
anchor_node_id and position. Keep instructions in execution order. Express every visible numbering or
reference change as an explicit replace instruction; Apply never invents additional document edits.

Writing task:
{to_prompt_json(writing_task)}

Document ({adapter.name}):
{model_input}
'''
        plan = self._call_llm_structured(prompt, ModifyPlan)
        node_ids = {block.node_id for block in self._iter_blocks(doc.blocks)}
        referenced = plan.target_node_ids + [i.target_node_id for i in plan.instructions]
        referenced += [i.anchor_node_id for i in plan.instructions if i.anchor_node_id]
        missing = [nid for nid in referenced if nid not in node_ids]
        if missing:
            raise ValueError(f'modify plan targets node_ids absent from WriterDocument: {missing}')
        return plan

    def _generate_patch(self, adapter, modify_plan: Any, document: Any) -> PatchSet:
        doc = self._unified_model(document, WriterDocument)
        plan = self._unified_model(modify_plan, ModifyPlan)
        model_input = adapter.document_to_model_input(doc)
        node_map, _ = self._build_node_index(doc.blocks)
        range_guide = {
            instruction.target_node_id: {
                'characters': {
                    index: character
                    for index, character in enumerate(node_map[instruction.target_node_id].content)
                },
                'end': len(node_map[instruction.target_node_id].content),
            }
            for instruction in plan.instructions if instruction.modify_type == 'replace'
        }
        prompt = f'''You are compiling an approved ModifyPlan into an executable PatchSet using
the {adapter.name} representation.
Produce exactly one PatchHunk for each ModifyInstruction, in the same order. Copy target_node_id,
modify_type, anchor_node_id, and position from the instruction. For replace, text_range is a Python
Unicode character range [start, end) in WriterBlock.content and new_text contains only the replacement
for that range, never the complete block. Count Unicode characters from zero and verify that
0 <= start <= end <= len(content). If the user quotes the source text, the range must cover exactly
that quoted text; do not include adjacent whitespace or punctuation. For insert, use new_blocks; do
not use new_text. A heading PatchBlock requires numbering={{"level": 1..9}} and a list_item requires
numbering={{"ordered": true|false}}. For delete, identify one target block only. For move, identify one
target block and its anchor. Omit old_text; the Adapter fills it from the original WriterDocument. Do
not include projection syntax in new_text.

Modify plan:
{to_prompt_json(plan)}

Character indexes for replace targets:
{to_prompt_json(range_guide)}

Document ({adapter.name}):
{model_input}
'''
        model_output = self._call_llm_structured(prompt, PatchSet)
        patch = adapter.model_output_to_patch(model_output, doc)
        if len(patch.hunks) != len(plan.instructions):
            raise ValueError('PatchSet must contain exactly one hunk per ModifyInstruction')
        for index, (hunk, instruction) in enumerate(zip(patch.hunks, plan.instructions)):
            expected = (
                instruction.target_node_id, instruction.modify_type,
                instruction.anchor_node_id, instruction.position,
            )
            actual = (
                hunk.target_node_id, hunk.modify_type, hunk.anchor_node_id, hunk.position,
            )
            if actual != expected:
                raise ValueError(f'Patch hunk-{index} does not match its ModifyInstruction')
        return patch

    # ==================== 私有：Patch 应用引擎 ====================

    def _apply_patch_atomically(self, document: WriterDocument, patch: PatchSet) -> str | None:
        node_map, _ = self._build_node_index(document.blocks)
        replacements: Dict[str, List[Tuple[int, PatchHunk]]] = {}
        for index, hunk in enumerate(patch.hunks):
            target = node_map.get(hunk.target_node_id)
            if target is None:
                return f'hunk-{index}'
            if hunk.modify_type == 'replace':
                start, end = hunk.text_range
                if (
                    start < 0 or end < start or end > len(target.content)
                    or target.content[start:end] != hunk.old_text
                ):
                    return f'hunk-{index}'
                replacements.setdefault(hunk.target_node_id, []).append((index, hunk))
            elif hunk.modify_type == 'delete' and target.content != hunk.old_text:
                return f'hunk-{index}'
            elif hunk.modify_type == 'move':
                if hunk.anchor_node_id not in node_map or hunk.anchor_node_id == hunk.target_node_id:
                    return f'hunk-{index}'

        for hunks in replacements.values():
            ordered = sorted(hunks, key=lambda item: item[1].text_range[0])
            for (_, previous), (index, current) in zip(ordered, ordered[1:]):
                if previous.text_range[1] > current.text_range[0]:
                    return f'hunk-{index}'

        for node_id, hunks in replacements.items():
            block = node_map[node_id]
            for _, hunk in sorted(hunks, key=lambda item: item[1].text_range[0], reverse=True):
                self._replace_text_range(block, *hunk.text_range, hunk.new_text or '')

        for index, hunk in enumerate(patch.hunks):
            if hunk.modify_type == 'replace':
                continue
            node_map, container_of = self._build_node_index(document.blocks)
            if not self._apply_block_hunk(hunk, node_map, container_of):
                return f'hunk-{index}'
        return None

    def _apply_block_hunk(
        self, hunk: PatchHunk, node_map: Dict[str, WriterBlock],
        container_of: Dict[str, List[WriterBlock]],
    ) -> bool:
        target = node_map.get(hunk.target_node_id)
        container = container_of.get(hunk.target_node_id)
        if target is None or container is None:
            return False
        if hunk.modify_type == 'delete':
            container.remove(target)
            return True
        if hunk.modify_type == 'insert':
            insert_at = container.index(target) + (hunk.position == 'after')
            blocks = [self._patch_block_to_writer(block, target.stage) for block in hunk.new_blocks]
            container[insert_at:insert_at] = blocks
            return True
        if hunk.modify_type == 'move':
            anchor = node_map.get(hunk.anchor_node_id)
            anchor_container = container_of.get(hunk.anchor_node_id)
            if anchor is None or anchor_container is None:
                return False
            moved_ids = {block.node_id for block in self._iter_blocks([target])}
            if anchor.node_id in moved_ids:
                return False
            container.remove(target)
            insert_at = anchor_container.index(anchor) + (hunk.position == 'after')
            anchor_container.insert(insert_at, target)
            return True
        return False

    @classmethod
    def _build_node_index(cls, roots: List[WriterBlock]):
        node_map: Dict[str, WriterBlock] = {}
        container_of: Dict[str, List[WriterBlock]] = {}

        def walk(blocks):
            for block in blocks:
                node_map[block.node_id] = block
                container_of[block.node_id] = blocks
                walk(block.children)
        walk(roots)
        return node_map, container_of

    @classmethod
    def _patch_block_to_writer(cls, block: PatchBlock, default_stage: WriterStage) -> WriterBlock:
        return WriterBlock(
            node_id=f'local-{uuid4().hex}', type=block.type,
            content=block.content, stage=default_stage, numbering=block.numbering,
        )

    @staticmethod
    def _replace_text_range(block: WriterBlock, start: int, end: int, new_text: str) -> None:
        new_content = block.content[:start] + new_text + block.content[end:]
        if not block.spans:
            block.content = new_content
            return
        old_styles = [style for span in block.spans for style in [tuple(span.style)] * len(span.text)]
        replaced_styles = old_styles[start:end]
        replacement_style = (
            replaced_styles[0]
            if replaced_styles and all(style == replaced_styles[0] for style in replaced_styles)
            else ()
        )
        new_styles = old_styles[:start] + [replacement_style] * len(new_text) + old_styles[end:]
        spans: List[WriterSpan] = []
        for character, style in zip(new_content, new_styles):
            if spans and tuple(spans[-1].style) == style:
                spans[-1].text += character
            else:
                spans.append(WriterSpan(text=character, style=list(style)))
        block.content = new_content
        block.spans = spans

    # ==================== 私有：飞书写回 ====================

    def _write_patch_to_feishu(self, feishu_url: str, patch_set: Any) -> PatchResult:
        '''把统一 PatchSet 写回飞书。'''
        patch = self._unified_model(patch_set, PatchSet)
        fs, real_path = self._resolve_feishu(feishu_url)
        resolved = fs.resolve_link(real_path)
        document_id = resolved.get('obj_token')
        if not document_id:
            raise ValueError(f'cannot resolve feishu url to a document: {feishu_url!r}')
        if patch.target_doc_id != document_id:
            raise ValueError(
                f'patch targets {patch.target_doc_id!r}, not Feishu document {document_id!r}'
            )

        raw_blocks = fs._get_doc_blocks_raw(document_id, with_descendants=True)
        source = WriterDocument(
            document_id=document_id, stage='final',
            title=resolved.get('title') or '', blocks=self._build_writer_blocks(raw_blocks),
        )
        _, local_result = self.apply_patch(source, patch)
        if not local_result.success:
            local_result.meta.update({'adapter': 'feishu', 'writeback_skipped': True})
            return local_result

        block_by_id = {block['block_id']: block for block in raw_blocks}
        for hunk in patch.hunks:
            target = block_by_id[hunk.target_node_id]
            if hunk.modify_type == 'replace' and target.get('block_type') not in _FEISHU_TEXT_FIELDS:
                raise ValueError(f'Feishu block is not text-editable: {hunk.target_node_id!r}')
            if hunk.modify_type == 'insert':
                for block in hunk.new_blocks:
                    self._patch_block_to_feishu(block)
            if hunk.modify_type in ('insert', 'delete'):
                parent = block_by_id.get(target.get('parent_id'))
                if parent is None or hunk.target_node_id not in parent.get('children', []):
                    raise ValueError(f'Feishu block has no writable parent: {hunk.target_node_id!r}')
            elif hunk.modify_type == 'move':
                anchor = block_by_id[hunk.anchor_node_id]
                if target.get('parent_id') != anchor.get('parent_id'):
                    raise ValueError('Feishu block_move_after currently requires source and anchor siblings')

        remote_node_ids: Dict[str, Any] = {}
        indexed_hunks = list(enumerate(patch.hunks))
        replacements = sorted(
            (item for item in indexed_hunks if item[1].modify_type == 'replace'),
            key=lambda item: (item[1].target_node_id, -item[1].text_range[0]),
        )
        structural = [item for item in indexed_hunks if item[1].modify_type != 'replace']
        for idx, hunk in replacements + structural:
            hunk_id = f'hunk-{idx}'
            raw_blocks = fs._get_doc_blocks_raw(document_id, with_descendants=True)
            block_by_id = {block['block_id']: block for block in raw_blocks}
            target = block_by_id[hunk.target_node_id]

            current_text, _ = self._extract_feishu_text(target)
            if hunk.modify_type == 'replace':
                start, end = hunk.text_range
                if current_text[start:end] != hunk.old_text:
                    raise ValueError(f'Feishu text changed before writeback: {hunk.target_node_id!r}')
                block = self._to_writer_block(target)
                self._replace_text_range(block, start, end, hunk.new_text or '')
                self._update_remote_text(fs, document_id, block)
            elif hunk.modify_type == 'move':
                self._move_remote_blocks(fs, document_id, hunk, target, block_by_id)
            elif hunk.modify_type == 'insert':
                parent_id = target['parent_id']
                parent = block_by_id[parent_id]
                target_index = parent['children'].index(hunk.target_node_id)
                insert_index = target_index + (hunk.position == 'after')
                created_ids = self._create_remote_blocks(
                    fs, document_id, parent_id, insert_index, hunk.new_blocks,
                )
                remote_node_ids[hunk_id] = created_ids[0] if len(created_ids) == 1 else created_ids
            elif hunk.modify_type == 'delete':
                if current_text != hunk.old_text:
                    raise ValueError(f'Feishu block changed before writeback: {hunk.target_node_id!r}')
                parent_id = target['parent_id']
                parent = block_by_id[parent_id]
                target_index = parent['children'].index(hunk.target_node_id)
                children_url = (
                    f'{fs._base_url}/docx/v1/documents/{document_id}'
                    f'/blocks/{parent_id}/children'
                )
                fs._delete(f'{children_url}/batch_delete', json={
                    'start_index': target_index, 'end_index': target_index + 1,
                })
            time.sleep(0.4)
        result = self._patch_result(
            patch, [f'hunk-{index}' for index in range(len(patch.hunks))], [],
        )
        result.meta.update({
            'adapter': 'feishu',
            'model_representation': patch.meta.get('model_representation'),
            'writeback_mode': 'docx_block_api',
            'remote_node_ids': remote_node_ids,
        })
        return result

    @classmethod
    def _move_remote_blocks(
        cls, fs, document_id: str, hunk: PatchHunk, target, block_by_id,
    ) -> str:
        anchor = block_by_id[hunk.anchor_node_id]
        parent_id = target.get('parent_id')
        parent = block_by_id.get(parent_id)
        if parent is None or anchor.get('parent_id') != parent_id:
            raise ValueError('Feishu block_move_after currently requires source and anchor siblings')

        child_ids = parent.get('children', [])
        remaining = [node_id for node_id in child_ids if node_id != hunk.target_node_id]
        anchor_index = remaining.index(anchor['block_id'])
        if hunk.position == 'after':
            destination_id = anchor['block_id']
        elif hunk.position == 'before':
            destination_id = remaining[anchor_index - 1] if anchor_index else parent_id
        else:
            raise ValueError('Feishu block_move_after supports sibling before/after positions only')

        fs._put(f'{fs._base_url}/docs_ai/v1/documents/{document_id}', json={
            'block_id': destination_id,
            'command': 'block_move_after',
            'format': 'xml',
            'revision_id': -1,
            'src_block_ids': hunk.target_node_id,
        })
        return hunk.target_node_id

    @classmethod
    def _create_remote_blocks(
        cls, fs, document_id: str, parent_id: str, index: int, blocks: List[PatchBlock],
    ) -> List[str]:
        children_url = (
            f'{fs._base_url}/docx/v1/documents/{document_id}/blocks/{parent_id}/children'
        )
        response = fs._post(children_url, json={
            'index': index,
            'children': [cls._patch_block_to_feishu(block) for block in blocks],
        })
        created = response.get('data', {}).get('children', [])
        if len(created) != len(blocks) or any(not block.get('block_id') for block in created):
            raise RuntimeError('Feishu did not return every created block ID')
        return [block['block_id'] for block in created]

    @classmethod
    def _patch_block_to_feishu(cls, block: PatchBlock) -> Dict[str, Any]:
        if block.type == 'heading':
            level = int(block.numbering.get('level', 1))
            if not 1 <= level <= 9:
                raise ValueError(f'Feishu heading level must be 1-9, got {level}')
            block_type = level + 2
        elif block.type == 'list_item':
            block_type = 13 if block.numbering.get('ordered') else 12
        else:
            block_type = _WRITER_TYPE_TO_FEISHU.get(block.type)
        if block_type is None or block_type == 1:
            raise ValueError(f'unsupported inserted Feishu block type: {block.type!r}')
        text_field = _FEISHU_TEXT_FIELDS[block_type]
        return {
            'block_type': block_type,
            text_field: {'elements': cls._spans_to_feishu_elements(block.content, [])},
        }

    @staticmethod
    def _spans_to_feishu_elements(content: str, spans: List[WriterSpan]) -> List[Dict[str, Any]]:
        source_spans = spans or [WriterSpan(text=content)]
        elements = []
        for span in source_spans:
            text_run: Dict[str, Any] = {'content': span.text}
            style = {
                name: True for name in ('bold', 'italic', 'underline', 'strikethrough', 'inline_code')
                if name in span.style
            }
            if style:
                text_run['text_element_style'] = style
            elements.append({'text_run': text_run})
        return elements

    @classmethod
    def _update_remote_text(cls, fs, document_id: str, block: WriterBlock) -> None:
        url = f'{fs._base_url}/docx/v1/documents/{document_id}/blocks/{block.node_id}'
        fs._patch(url, json={'update_text_elements': {
            'elements': cls._spans_to_feishu_elements(block.content, block.spans),
        }})

    # ==================== 私有：飞书读取与构造 ====================

    def _resolve_feishu(self, feishu_url: str):
        if not feishu_url.strip():
            raise ValueError('feishu_url is empty; fill FEISHU_DOCUMENT_URL in test.py')
        import lazyllm.tools.fs.client as _fs_client
        protocol, space_id, real_path = _fs_client.FS._parse(feishu_url)
        if protocol != 'feishu':
            raise ValueError(f'expected a Feishu URL, got protocol {protocol!r}')

        # 本地 POC 注入 app_id/app_secret 创建的 FeishuFS；
        # LazyMind 产品运行时未注入时，回退到 FS router 的动态用户 token 鉴权。
        configured_fs = self.adapters.get('feishu')
        if configured_fs is not None:
            return configured_fs, real_path
        return _fs_client.FS._get_or_create_fs(protocol, space_id, real_path), real_path

    @classmethod
    def _build_writer_blocks(cls, raw_blocks: List[Dict[str, Any]]) -> List[WriterBlock]:
        pairs = [(raw, cls._to_writer_block(raw)) for raw in raw_blocks if raw.get('block_id')]
        by_id = {block.node_id: block for _, block in pairs}
        roots: List[WriterBlock] = []
        for raw, block in pairs:
            parent_id = str(raw.get('parent_id') or '')
            if parent_id in by_id:
                by_id[parent_id].children.append(block)
            else:
                roots.append(block)
        return roots

    @classmethod
    def _to_writer_block(cls, raw: Dict[str, Any]) -> WriterBlock:
        raw_type = raw.get('block_type', 'block')
        block_type = _FEISHU_TYPE_MAP.get(raw_type, raw_type) if isinstance(raw_type, int) else str(raw_type)
        level = raw.get('level')
        if level is None and isinstance(raw_type, int) and 3 <= raw_type <= 11:
            level = raw_type - 2
        numbering = {'level': level} if level is not None else {}
        if raw_type in (12, 13):
            numbering['ordered'] = raw_type == 13
        source_refs = [{
            'source_block_id': str(raw.get('block_id', '')),
            'source_block_type': raw_type,
            'parent_id': str(raw.get('parent_id', '')),
        }]
        content, spans = cls._extract_feishu_text(raw)
        return WriterBlock(
            node_id=str(raw.get('block_id', '')),
            type=block_type,
            content=content,
            spans=spans,
            stage='final',
            numbering=numbering,
            source_refs=source_refs,
        )

    @staticmethod
    def _extract_feishu_text(raw: Dict[str, Any]) -> Tuple[str, List[WriterSpan]]:
        text_field = _FEISHU_TEXT_FIELDS.get(raw.get('block_type'))
        if text_field is None:
            return '', []
        elements = raw.get(text_field, {}).get('elements', [])
        spans: List[WriterSpan] = []
        for element in elements:
            text_run = element.get('text_run')
            if text_run is None:
                continue
            style_data = text_run.get('text_element_style', {})
            styles = [
                name for name in ('bold', 'italic', 'underline', 'strikethrough', 'inline_code')
                if style_data.get(name) is True
            ]
            if style_data.get('link'):
                styles.append('link')
            spans.append(WriterSpan(text=text_run.get('content', ''), style=styles))
        return ''.join(span.text for span in spans), spans

    @classmethod
    def _iter_blocks(cls, blocks: List[WriterBlock]):
        for block in blocks:
            yield block
            yield from cls._iter_blocks(block.children)

    @staticmethod
    def _patch_result(
        patch: PatchSet, applied: List[str], failed: List[str], message: str | None = None,
    ) -> PatchResult:
        return PatchResult(
            patch_id=patch.patch_id,
            success=not failed,
            applied_hunks=applied,
            failed_hunks=failed,
            message=message or ('Patch applied.' if not failed else f'{len(failed)} hunk(s) failed.'),
        )
