import tempfile
from copy import copy
from pathlib import Path
from unittest.mock import ANY, patch

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
    WriterBlock,
    WriterDocument,
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
            'visual_type': 'image',
            'purpose': '视觉计划不应保留在短文写作计划中',
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        tool = WriterPlanningTools(artifact_store=directory)
        with patch.object(tool, '_call_llm_structured', return_value=model_plan) as mocked:
            result = tool.generate_short_writing_plan(task=task, context=context)
        plan = load_artifact_json(result['artifact_path'], ShortWritingPlan)

    prompt = mocked.call_args.args[0]
    assert 'Visuals are planned separately' in prompt
    assert plan.instruction_id == 'ctx-short-short-writing-plan'
    assert plan.content_ref == ContentRef(document_root=True)
    assert plan.section_title == '新能源汽车降价背后的市场变化'
    assert plan.core_viewpoint == model_plan.core_viewpoint
    assert plan.references == [{'id': 'fact-1'}]
    assert plan.visual_needs == []
    assert plan.meta['target_chars'] == 700
    assert plan.meta['max_chars'] == 800


def test_generate_short_visual_plan_uses_visual_plan_schema_and_document_root():
    task, context, plan = _short_inputs()
    model_plan = VisualPlan(instructions=[VisualInstruction(
        need_id='model-generated-id',
        content_ref=ContentRef(document_root=True),
        visual_type='image',
        purpose='  展示降价原因和消费者影响  ',
        preferred_strategy=None,
        required=True,
        meta={'placement_hint': '  分析消费者机会之后  '},
    )])

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterPlanningTools(artifact_store=directory)
        with patch.object(tool, '_call_llm_structured', return_value=model_plan) as mocked:
            result = tool.generate_short_visual_plan(
                task=task,
                short_writing_plan=plan,
                context=context,
            )
        visual_plan = load_artifact_json(result['artifact_path'], VisualPlan)

    prompt, schema = mocked.call_args.args
    assert schema is VisualPlan
    assert 'meta.placement_hint' in prompt
    assert 'Never put placement guidance' in prompt
    assert visual_plan.instructions == [VisualInstruction(
        need_id='visual-document-1',
        content_ref=ContentRef(document_root=True),
        visual_type='image',
        purpose='展示降价原因和消费者影响',
        preferred_strategy=None,
        required=True,
        meta={'placement_hint': '分析消费者机会之后'},
    )]


def test_generate_short_visual_plan_retries_invalid_strategy_inside_structured_call():
    task, context, plan = _short_inputs()
    responses = iter([
        {
            'instructions': [{
                'need_id': 'visual-1',
                'content_ref': {'document_root': True},
                'visual_type': 'image',
                'purpose': '展示消费者购车决策因素',
                'preferred_strategy': '分析降价原因之后插入',
            }],
        },
        {
            'instructions': [{
                'need_id': 'visual-1',
                'content_ref': {'document_root': True},
                'visual_type': 'image',
                'purpose': '展示消费者购车决策因素',
                'preferred_strategy': None,
                'meta': {'placement_hint': '分析降价原因之后插入'},
            }],
        },
    ])
    calls = []

    def model(_prompt):
        calls.append('call')
        return next(responses)

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterPlanningTools(llm=object(), artifact_store=directory)
        with patch.object(tool, '_build_structured_llm', return_value=model):
            result = tool.generate_short_visual_plan(
                task=task,
                short_writing_plan=plan,
                context=context,
            )
        visual_plan = load_artifact_json(result['artifact_path'], VisualPlan)

    assert calls == ['call', 'call']
    assert visual_plan.instructions[0].preferred_strategy is None
    assert visual_plan.instructions[0].meta['placement_hint'] == '分析降价原因之后插入'


def test_short_visual_plan_trace_records_raw_response_and_validation_failure():
    responses = iter([
        'not-json',
        '{"instructions": []}',
    ])

    def model(_prompt):
        return next(responses)

    tool = WriterPlanningTools(llm=object())
    with (
        patch.object(tool, '_build_structured_llm', return_value=model) as build_model,
        patch('lazyllm.tools.writer.tools.base.start_span', side_effect=['span-1', 'span-2']) as start,
        patch('lazyllm.tools.writer.tools.base.set_span_output') as set_output,
        patch('lazyllm.tools.writer.tools.base.set_span_attributes') as set_attributes,
        patch('lazyllm.tools.writer.tools.base.set_span_error') as set_error,
        patch('lazyllm.tools.writer.tools.base.finish_span') as finish,
    ):
        result = tool._call_llm_structured(
            'visual prompt',
            VisualPlan,
            trace_label='short_visual_plan',
        )

    assert result.instructions == []
    build_model.assert_called_once_with(
        ANY,
        stream_output=False,
        apply_formatter=False,
    )
    assert start.call_count == 2
    assert set_output.call_args_list[0].args == ('span-1', 'not-json')
    assert set_output.call_args_list[1].args == ('span-2', '{"instructions": []}')
    assert set_error.call_count == 1
    assert set_error.call_args.args[0] == 'span-1'
    assert finish.call_args_list[0].args == ('span-1',)
    assert finish.call_args_list[1].args == ('span-2',)
    recorded_attributes = [call.args[1] for call in set_attributes.call_args_list]
    assert any(attrs.get('writer.structured.failure_stage') == 'validation' for attrs in recorded_attributes)
    assert any(attrs.get('writer.structured.instruction_count') == 0 for attrs in recorded_attributes)


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


def test_generate_short_document_ir_saves_flat_editable_lmd():
    task, context, plan = _short_inputs()
    task.output = {'representation': 'ir'}
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'ir'
    model_document = WriterDocument(
        document_id='model-document',
        title='模型标题',
        stage='draft',
        blocks=[WriterBlock(
            node_id='paragraph-1',
            type='paragraph',
            content='新能源汽车降价降低了消费者的购车门槛。',
        )],
    )

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(artifact_store=directory)
        with patch.object(tool, '_call_llm_structured', return_value=model_document):
            result = tool.generate_short_document(
                task=task,
                short_writing_plan=plan,
                context=context,
            )
        document = load_artifact_json(result['artifact_path'], WriterDocument)

    assert Path(result['artifact_path']).suffix == '.lmd'
    assert document.title == task.target_document.title
    assert document.ui_editable is True
    assert document.blocks[0].type == 'paragraph'
    assert all(block.type != 'heading' for block in document.iter_blocks())
    assert result['metadata']['extra']['representation'] == 'ir'
    assert result['metadata']['extra']['structure_mode'] == 'flat'


def test_stream_short_document_ir_previews_markdown_and_returns_writer_document():
    task, context, plan = _short_inputs()
    task.output = {'representation': 'ir'}
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'ir'
    model_document = WriterDocument(
        document_id='model-document',
        title='模型标题',
        stage='draft',
        blocks=[WriterBlock(
            node_id='paragraph-1',
            type='paragraph',
            content='新能源汽车降价降低了消费者的购车门槛。',
        )],
    )

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(artifact_store=directory)
        with (
            patch.object(tool, '_call_llm_structured', return_value=model_document),
            tool.stream_short_document(
                task=task,
                short_writing_plan=plan,
                context=context,
                idle_timeout=1,
            ) as stream,
        ):
            preview = ''.join(stream)
            result = stream.result()
        document = load_artifact_json(result['artifact_path'], WriterDocument)

    assert preview == (
        '# 新能源汽车降价背后的市场变化\n\n'
        '新能源汽车降价降低了消费者的购车门槛。\n'
    )
    assert document.title == task.target_document.title
    assert all(block.type != 'heading' for block in document.iter_blocks())


@pytest.mark.parametrize('model_has_image', [True, False])
@pytest.mark.parametrize(
    ('local_path', 'expected_path'),
    [
        ('/tmp/short-ir-visual.png', '/tmp/short-ir-visual.png'),
        (None, 'https://example.com/short-ir-visual.png'),
    ],
)
def test_generate_short_document_ir_prefers_local_path_for_resolved_visual_asset(
    model_has_image,
    local_path,
    expected_path,
):
    task, context, plan = _short_inputs()
    task.output = {'representation': 'ir'}
    plan.content_ref = ContentRef(document_root=True)
    plan.section_title = task.target_document.title
    plan.meta['representation'] = 'ir'
    visual_plan = VisualPlan(instructions=[VisualInstruction(
        need_id='visual-document-1',
        content_ref=ContentRef(document_root=True),
        visual_type='image',
        purpose='展示消费者购车决策因素',
        required=True,
        meta={'placement_hint': '分析消费者机会之后'},
    )])
    media = MediaAssetLibrary(
        library_id='media-short-ir',
        assets={'asset-1': MediaAsset(
            media_asset_id='asset-1',
            asset_type='generated_image',
            source_type='image_generation',
            uri='https://example.com/short-ir-visual.png',
            local_path=local_path,
        )},
        visual_need_asset_ids={'visual-document-1': ['asset-1']},
    )
    blocks = [WriterBlock(
        node_id='paragraph-1',
        type='paragraph',
        content='消费者需要综合比较购车成本和后续服务。',
    )]
    if model_has_image:
        blocks.append(WriterBlock(
            node_id='visual-document-1',
            type='image',
            content='购车决策因素',
        ))
    model_document = WriterDocument(
        document_id='model-document',
        title=plan.section_title,
        stage='draft',
        blocks=blocks,
    )

    with tempfile.TemporaryDirectory() as directory:
        tool = WriterDraftingTools(artifact_store=directory)
        with patch.object(tool, '_call_llm_structured', return_value=model_document) as mocked:
            result = tool.generate_short_document(
                task=task,
                short_writing_plan=plan,
                context=context,
                visual_plan=visual_plan,
                media_assets=media,
            )
        document = load_artifact_json(result['artifact_path'], WriterDocument)

    image = next(block for block in document.iter_blocks() if block.type == 'image')
    assert image.references == [{
        'type': 'media_asset',
        'id': 'asset-1',
        'path': expected_path,
    }]
    assert 'asset-1' in mocked.call_args.args[0]
    assert expected_path in mocked.call_args.args[0]
    if local_path:
        assert 'https://example.com/short-ir-visual.png' not in mocked.call_args.args[0]


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
