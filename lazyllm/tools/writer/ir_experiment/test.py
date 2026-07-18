from __future__ import annotations

import pytest

from ..data_models.task import WritingTask
from .experiment_tools import IRExperimentTools
from .markdown_projection import MarkdownDocumentProjection
from .writer_ir import WriterBlock, WriterDocument, WriterSpan
from .xml_projection import XmlDocumentProjection


# Fill these credentials manually before running the live experiment.
FEISHU_DOCUMENT_URL = ''
FEISHU_APP_ID = ''
FEISHU_APP_SECRET = ''
# 当前 FeishuWikiFS 的文档 Block 接口需要知识库 space_id，例如 wikcn... 。
FEISHU_WIKI_SPACE_ID = ''
QWEN_API_KEY = ''
LLM_MODEL = ''
WRITE_BACK = False

REVISION_QUERY = (
    '1. 将第三章开头段落中的“三位一体的协同进化”替换为“三要素的协同演进”，只修改这一处。\n'
    # '2. 仅将“四、产业应用与社会影响”下“2. 社会影响与伦理治理”中“对齐与安全控制”条目里的“人工智能监管”替换为“AI 监管”，不要修改其他位置的“人工智能”。\n'
    # '3. 在第三章开头段落之后、“1. 核心技术架构”之前插入一个新段落：“三类要素彼此制约，任何单一环节的瓶颈都会限制整体能力上限。”\n'
    # '4. 删除第四章“1. 主要产业应用场景”下的“内容创作（AIGC）”整个列表项，保持其他兄弟列表项的顺序不变。\n'
    # '5. 将第四章的“2. 社会影响与伦理治理”及其下全部三个列表项作为一个子树，移动到“1. 主要产业应用场景”之前。\n'
    # '6. 在第四章之后插入新章节“五、AI 治理与合规实践”及一段简介，并将原第五章和第六章依次重排为第六章和第七章，保持各章内部编号连续。\n'
    # '7. 将章节标题“三、当前技术版图与核心驱动力”修改为“三、技术版图与发展动能”，并同步将引言中的“当前技术版图、核心驱动力”更新为“技术版图、发展动能”。\n'
    # '8. 将第二章第一阶段列表中加粗的“主流范式：”改为“核心范式：”，只修改这部分文字，保持其加粗样式以及后续正文的原有样式。\n'
    # '9. 仅根据文档目录和第五章相关片段，将“3. 能源与硬件极限”段落压缩为不超过 80 字的摘要，不向模型提供其他章节全文。\n'
    # '10. 从空文档开始，围绕“人工智能的历史、技术、产业影响与 AGI 展望”先生成包含引言、历史演进、技术版图、产业应用、前沿挑战和结语的 Outline，再逐章扩写为 Draft，最后润色为 Final。'
)


def _build_task(representation: str) -> WritingTask:
    return WritingTask(
        task_id=f'{representation}-experiment-1',
        task_type='revise',
        query=REVISION_QUERY,
    )


def _run_experiment(representation: str):
    if not FEISHU_DOCUMENT_URL:
        raise ValueError('Fill FEISHU_DOCUMENT_URL before running the experiment.')
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise ValueError('Fill FEISHU_APP_ID and FEISHU_APP_SECRET before running the experiment.')
    if not FEISHU_WIKI_SPACE_ID:
        raise ValueError('Fill FEISHU_WIKI_SPACE_ID required by the current FeishuWikiFS block API.')
    if not QWEN_API_KEY:
        raise ValueError('Fill QWEN_API_KEY before running the experiment.')

    import lazyllm
    from lazyllm.tools.fs.supplier.feishu import FeishuFS

    llm_kwargs = {'source': 'qwen', 'api_key': QWEN_API_KEY, 'stream': False}
    if LLM_MODEL:
        llm_kwargs['model'] = LLM_MODEL
    feishu_fs = FeishuFS(
        app_id=FEISHU_APP_ID, app_secret=FEISHU_APP_SECRET,
        space_id=FEISHU_WIKI_SPACE_ID, dynamic_auth=False,
    )
    tool = IRExperimentTools(
        llm=lazyllm.OnlineChatModule(**llm_kwargs),
        adapters={'feishu': feishu_fs},
    )

    document = tool.read_writer_doc(FEISHU_DOCUMENT_URL)
    modify_plan = getattr(tool, f'generate_modify_plan_{representation}')(
        _build_task(representation), document,
    )
    patch_set = getattr(tool, f'generate_patch_{representation}')(modify_plan, document)
    revised_doc, apply_result = tool.apply_patch(document, patch_set)
    write_result = None
    if WRITE_BACK and apply_result.success:
        write_result = tool.write_patch_to_feishu(FEISHU_DOCUMENT_URL, patch_set)

    print('ModifyPlan:')
    print(modify_plan.model_dump_json(indent=2))
    print('PatchSet:')
    print(patch_set.model_dump_json(indent=2))
    print('Local apply:')
    print(apply_result.model_dump_json(indent=2))
    if write_result:
        print('Feishu writeback:')
        print(write_result.model_dump_json(indent=2))
    return {
        'source_doc': document, 'modify_plan': modify_plan, 'patch_set': patch_set,
        'revised_doc': revised_doc, 'apply_result': apply_result,
        'write_result': write_result,
    }


def run_json_experiment():
    return _run_experiment('json')


def run_xml_experiment():
    return _run_experiment('xml')


def run_markdown_experiment():
    return _run_experiment('markdown')


def _rich_style_document() -> WriterDocument:
    spans = [
        WriterSpan(text='普通文本，'),
        WriterSpan(text='加粗', style=['bold']),
        WriterSpan(text='、'),
        WriterSpan(text='斜体', style=['italic']),
        WriterSpan(text='、'),
        WriterSpan(text='下划线', style=['underline']),
        WriterSpan(text='、'),
        WriterSpan(text='删除线', style=['strikethrough']),
        WriterSpan(text='、'),
        WriterSpan(text='行内代码', style=['inline_code']),
    ]
    paragraph = WriterBlock(
        node_id='paragraph-1', type='paragraph',
        content=''.join(span.text for span in spans), spans=spans, stage='final',
    )
    nested_item = WriterBlock(
        node_id='list-item-2', type='list_item', content='嵌套列表项',
        stage='final', numbering={'ordered': False},
    )
    list_item = WriterBlock(
        node_id='list-item-1', type='list_item', content='有序列表项',
        children=[nested_item], stage='final', numbering={'ordered': True},
    )
    root = WriterBlock(
        node_id='document-root', type='document', content='投影测试文档', stage='final',
        children=[
            WriterBlock(
                node_id='heading-1', type='heading', content='二级标题',
                stage='final', numbering={'level': 2},
            ),
            paragraph,
            list_item,
            WriterBlock(
                node_id='quote-1', type='quote', content='引用内容', stage='final',
            ),
            WriterBlock(
                node_id='code-1', type='code', content='print("hello")', stage='final',
            ),
        ],
    )
    return WriterDocument(
        document_id='projection-test-document', stage='final',
        title='投影测试文档', blocks=[root],
    )


def _editable_semantics(document: WriterDocument, markdown: bool = False):
    supported_styles = {'bold', 'italic', 'strikethrough', 'inline_code'}

    def block_semantics(block: WriterBlock):
        style_by_character = []
        spans = block.spans or [WriterSpan(text=block.content)]
        for span in spans:
            styles = set(span.style)
            if markdown:
                styles &= supported_styles
            style_by_character.extend([tuple(sorted(styles))] * len(span.text))
        return {
            'node_id': block.node_id,
            'type': block.type,
            'content': block.content,
            'styles': style_by_character,
            'numbering': {
                key: value for key, value in block.numbering.items()
                if key in {'level', 'ordered'}
            },
            'children': [block_semantics(child) for child in block.children],
        }

    return {
        'document_id': document.document_id,
        'title': document.title,
        'blocks': [block_semantics(block) for block in document.blocks],
    }


def test_projection_round_trip():
    document = _rich_style_document()
    xml_restored = XmlDocumentProjection.parse(
        str(XmlDocumentProjection.from_writer_document(document))
    ).to_writer_document()
    markdown_restored = MarkdownDocumentProjection.parse(
        str(MarkdownDocumentProjection.from_writer_document(document))
    ).to_writer_document()

    assert _editable_semantics(xml_restored) == _editable_semantics(document)
    assert _editable_semantics(markdown_restored, markdown=True) == _editable_semantics(
        document, markdown=True,
    )


@pytest.mark.skipif(
    not (
        FEISHU_DOCUMENT_URL
        and FEISHU_APP_ID
        and FEISHU_APP_SECRET
        and FEISHU_WIKI_SPACE_ID
        and QWEN_API_KEY
    ),
    reason='Fill live Feishu and Qwen credentials in test.py.',
)
@pytest.mark.parametrize('representation', ['json', 'xml', 'markdown'])
def test_projection_routes_live(representation):
    result = _run_experiment(representation)
    assert result['modify_plan'].instructions
    assert result['patch_set'].hunks
    assert result['apply_result'].success is True
    if not WRITE_BACK:
        return

    assert result['write_result'].success is True
