import tempfile
from copy import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from lazyllm.module.module import ModuleBase
from lazyllm.tools.writer.data_models import (
    ContentRef,
    DocumentFact,
    MediaAsset,
    MediaAssetLibrary,
    SectionInstruction,
    ShortWritingPlan,
    TargetDocument,
    VisualInstruction,
    VisualPlan,
    WritingContext,
    WritingTask,
)
from lazyllm.tools.writer.tools.drafting_tools import WriterDraftingTools
from lazyllm.tools.writer.tools.planning_tools import WriterPlanningTools
from lazyllm.tools.writer.utils import load_artifact_json


class _StreamingTextLLM(ModuleBase):
    def __init__(self, chunks):
        super().__init__()
        self._chunks = chunks
        self._stream = False

    def share(self, stream=None):
        shared = copy(self)
        if stream is not None:
            shared._stream = stream
        return shared

    def forward(self, prompt):
        with self.stream_output(self._stream):
            for delta in self._chunks:
                self._stream_output(delta, cls='text')
        return ''.join(self._chunks)


def _short_inputs():
    task = WritingTask(
        task_id='task-short',
        query='写一篇连续正文，不使用小标题，约700字',
        task_type='write',
        target_document=TargetDocument(title='新能源汽车降价背后的市场变化'),
        constraints={
            'structure_mode': 'flat',
            'target_chars': 700,
            'max_chars': 800,
        },
        output={'representation': 'markdown'},
    )
    context = WritingContext(
        context_id='ctx-short',
        facts=[DocumentFact(
            fact_id='fact-1',
            key='竞争',
            value='市场竞争加剧',
            source=['resource-1'],
        )],
    )
    plan = ShortWritingPlan(
        instruction_id='model-id',
        content_ref=ContentRef(node_id='wrong-node'),
        section_title='模型标题',
        section_goal='解释降价原因及其对消费者的影响。',
        core_viewpoint='降价带来机会，也伴随服务和保值率风险。',
        required_points=['市场竞争加剧', '消费者购车成本下降'],
        references=[{'id': 'fact-1'}, {'id': 'invented'}],
        fact_constraints=['市场竞争加剧'],
        style_constraints=['客观、通俗、克制'],
        expected_blocks=['现象切入', '原因分析', '消费者建议'],
    )
    return task, context, plan


def test_short_writing_plan_schema_differs_only_for_flat_document_needs():
    assert 'core_viewpoint' in ShortWritingPlan.model_fields
    assert 'relation_constraints' not in ShortWritingPlan.model_fields
    assert 'references' in ShortWritingPlan.model_fields
    assert 'relation_constraints' in SectionInstruction.model_fields
    assert 'core_viewpoint' not in SectionInstruction.model_fields


def test_generate_short_writing_plan_targets_document_root_and_keeps_valid_references():
    task, context, model_plan = _short_inputs()
    model_plan.visual_needs = [
        {
            'need_id': 'model-invented-id',
            'content_ref': {'node_id': 'model-invented-target'},
            'visual_type': 'image',
            'purpose': '  展示降价原因和消费者影响  ',
            'required': False,
            'placement_hint': '分析消费者机会之后',
        },
        {
            'visual_type': 'diagram',
            'purpose': '说明购车决策因素',
            'required': True,
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        tool = WriterPlanningTools(artifact_store=directory)
        with patch.object(tool, '_call_llm_structured', return_value=model_plan) as mocked:
            result = tool.generate_short_writing_plan(task=task, context=context)
        plan = load_artifact_json(result['artifact_path'], ShortWritingPlan)

    prompt = mocked.call_args.args[0]
    assert 'materially improves' in prompt
    assert 'Do not impose a visual' in prompt
    assert plan.instruction_id == 'ctx-short-short-writing-plan'
    assert plan.content_ref == ContentRef(document_root=True)
    assert plan.section_title == '新能源汽车降价背后的市场变化'
    assert plan.core_viewpoint == model_plan.core_viewpoint
    assert plan.references == [{'id': 'fact-1'}]
    assert plan.visual_needs == [
        {
            'need_id': 'visual-document-1',
            'content_ref': {'document_root': True},
            'visual_type': 'image',
            'purpose': '展示降价原因和消费者影响',
            'required': False,
            'meta': {'placement_hint': '分析消费者机会之后'},
        },
        {
            'need_id': 'visual-document-2',
            'content_ref': {'document_root': True},
            'visual_type': 'diagram',
            'purpose': '说明购车决策因素',
            'required': True,
        },
    ]
    assert plan.meta['target_chars'] == 700
    assert plan.meta['max_chars'] == 800


def test_generate_short_document_has_one_title_and_no_section_headings():
    task, context, plan = _short_inputs()
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'markdown'
    body = '## 降价原因\n市场竞争加剧，部分成本发生变化。\n\n## 消费者建议\n消费者还要关注服务和保值率。'

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(llm=lambda _: body, artifact_store=directory)
        result = tool.generate_short_document(task=task, short_writing_plan=plan, context=context)
        markdown = Path(result['artifact_path']).read_text(encoding='utf-8')

    assert markdown.startswith('# 新能源汽车降价背后的市场变化\n\n')
    assert '\n## ' not in markdown
    assert '降价原因\n市场竞争加剧' in markdown
    assert result['metadata']['extra']['structure_mode'] == 'flat'


def test_stream_short_document_emits_title_and_complete_flat_document():
    task, context, plan = _short_inputs()
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'markdown'

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(
            llm=_StreamingTextLLM(['第一段。', '\n\n第二段。']),
            artifact_store=directory,
        )
        with tool.stream_short_document(
            task=task,
            short_writing_plan=plan,
            context=context,
            idle_timeout=1,
        ) as stream:
            preview = ''.join(stream)
            result = stream.result()
        markdown = Path(result['artifact_path']).read_text(encoding='utf-8')

    assert preview == '# 新能源汽车降价背后的市场变化\n\n第一段。\n\n第二段。\n'
    assert markdown == preview
    assert '\n## ' not in markdown


def test_generate_short_document_places_only_resolved_planned_visuals():
    task, context, plan = _short_inputs()
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'markdown'
    visual_plan = VisualPlan(instructions=[VisualInstruction(
        need_id='visual-document-1',
        content_ref=ContentRef(document_root=True),
        visual_type='image',
        purpose='展示降价原因和消费者影响',
        meta={'placement_hint': '分析消费者机会之后'},
    )])
    media = MediaAssetLibrary(
        library_id='media-short',
        assets={
            'asset-1': MediaAsset(
                media_asset_id='asset-1',
                asset_type='generated_image',
                source_type='image_generation',
                local_path='/tmp/short-visual.png',
            ),
        },
        visual_need_asset_ids={'visual-document-1': ['asset-1']},
    )
    body = (
        '新能源汽车降价为消费者带来了更低的购车门槛。\n\n'
        '消费者也需要关注售后服务和保值率。'
    )

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(artifact_store=directory)
        with patch.object(tool, '_call_llm_text', return_value=body) as mocked:
            result = tool.generate_short_document(
                task=task,
                short_writing_plan=plan,
                context=context,
                visual_plan=visual_plan,
                media_assets=media,
            )
        markdown = Path(result['artifact_path']).read_text(encoding='utf-8')

    prompt = mocked.call_args.args[0]
    assert 'visual-document-1' in prompt
    assert '分析消费者机会之后' in prompt
    assert '/tmp/short-visual.png' not in prompt
    assert 'media-placeholder://visual-document-1' in markdown
    assert '\n## ' not in markdown


def test_generate_short_document_rejects_unplanned_image_url():
    task, context, plan = _short_inputs()
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'markdown'

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(
            llm=lambda _: '正文。\n\n![未规划图片](https://example.com/invented.png)',
            artifact_store=directory,
        )
        with pytest.raises(ValueError, match='Unplanned short-document image target'):
            tool.generate_short_document(task=task, short_writing_plan=plan, context=context)
