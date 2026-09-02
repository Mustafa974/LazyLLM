from copy import deepcopy

import pytest

from lazyllm.tools.writer.adapter.notion import NotionWriterAdapter
from lazyllm.tools.writer.data_models import (
    MediaAsset, MediaAssetLibrary, PatchHunk, WriterBlock, WriterDocument, WriterSpan,
)


DOC_ID = '01234567-89ab-cdef-0123-456789abcdef'
HEADING_ID = '11111111-1111-1111-1111-111111111111'
PARAGRAPH_ID = '22222222-2222-2222-2222-222222222222'
TOGGLE_ID = '33333333-3333-3333-3333-333333333333'
CHILD_ID = '44444444-4444-4444-4444-444444444444'


def _rich(text, **annotations):
    return {
        'type': 'text',
        'text': {'content': text, 'link': None},
        'annotations': {
            'bold': False,
            'italic': False,
            'strikethrough': False,
            'underline': False,
            'code': False,
            'color': 'default',
            **annotations,
        },
        'plain_text': text,
        'href': None,
    }


def _block(block_id, block_type, payload, *, parent=DOC_ID, has_children=False):
    return {
        'object': 'block',
        'id': block_id,
        'block_id': block_id,
        'type': block_type,
        'block_type': block_type,
        'parent': {'type': 'block_id', 'block_id': parent},
        'parent_id': parent,
        'has_children': has_children,
        block_type: payload,
    }


def test_blocks_to_ir_preserves_payload_bindings_and_input():
    raw = _block(PARAGRAPH_ID, 'paragraph', {
        'rich_text': [_rich('正文')],
        'color': 'default',
    })
    source = deepcopy(raw)

    document = NotionWriterAdapter().blocks_to_ir(
        [raw], external_document_id=DOC_ID, title='标题',
        uri=f'https://notion.so/{DOC_ID}', revision='revision-1')

    block = document.blocks[0]
    assert document.document_id == NotionWriterAdapter.make_document_id(DOC_ID)
    assert document.title == '标题'
    assert document.metadata == {'source_block_count': 1}
    assert document.provider_binding == {
        'provider': 'notion',
        'document_id': DOC_ID,
        'uri': f'https://notion.so/{DOC_ID}',
        'revision': 'revision-1',
    }
    assert document.ui_editable is False
    assert block.node_id == NotionWriterAdapter.make_node_id(DOC_ID, PARAGRAPH_ID)
    assert block.provider_binding == {
        'provider': 'notion',
        'document_id': DOC_ID,
        'block_id': PARAGRAPH_ID,
        'parent_block_id': DOC_ID,
        'revision': 'revision-1',
    }
    assert block.provider_payload == {'raw_block': source, 'source_index': 0}
    assert block.content == '正文'
    assert block.editable is True
    assert raw == source


def test_ir_to_blocks_flattens_logical_heading_children_as_notion_siblings():
    document = WriterDocument(
        document_id='writer-document',
        provider_binding={'provider': 'notion', 'document_id': DOC_ID},
        blocks=[WriterBlock(
            node_id='section-1', type='heading', content='章节',
            numbering={'level': 1},
            children=[
                WriterBlock(node_id='paragraph-1', type='paragraph', content='正文一'),
                WriterBlock(
                    node_id='section-1-1', type='heading', content='子章节',
                    numbering={'level': 2},
                    children=[WriterBlock(
                        node_id='paragraph-2', type='paragraph', content='正文二',
                    )],
                ),
            ],
        )],
    )

    native = NotionWriterAdapter().ir_to_blocks(document)

    assert [block['type'] for block in native] == [
        'heading_1', 'paragraph', 'heading_2', 'paragraph',
    ]
    assert all('children' not in block[block['type']] for block in native)


@pytest.mark.parametrize(('block_type', 'expected_type', 'numbering', 'editable'), [
    ('heading_1', 'heading', {'level': 1}, True),
    ('heading_2', 'heading', {'level': 2}, True),
    ('heading_3', 'heading', {'level': 3}, True),
    ('heading_4', 'heading', {'level': 4}, True),
    ('bulleted_list_item', 'list_item', {'ordered': False}, True),
    ('numbered_list_item', 'list_item', {'ordered': True}, True),
])
def test_maps_headings_and_lists(block_type, expected_type, numbering, editable):
    raw = _block(PARAGRAPH_ID, block_type, {'rich_text': [_rich('内容')]})

    block = NotionWriterAdapter().blocks_to_ir(
        [raw], external_document_id=DOC_ID).blocks[0]

    assert block.type == expected_type
    assert block.numbering == numbering
    assert block.editable is editable


def test_maps_rich_text_styles_links_mentions_and_equations():
    rich_text = [
        {
            **_rich('样式', bold=True, italic=True, underline=True,
                    strikethrough=True, code=True, color='red_background'),
            'text': {'content': '样式', 'link': {'url': 'https://example.com'}},
            'href': 'https://example.com',
        },
        {
            'type': 'mention',
            'mention': {'type': 'page', 'page': {'id': HEADING_ID}},
            'annotations': {'color': 'blue'},
            'plain_text': '页面',
            'href': None,
        },
        {
            'type': 'equation',
            'equation': {'expression': 'E=mc^2'},
            'annotations': {'color': 'default'},
            'plain_text': 'E=mc^2',
            'href': None,
        },
    ]
    raw = _block(PARAGRAPH_ID, 'paragraph', {'rich_text': rich_text})

    block = NotionWriterAdapter().blocks_to_ir(
        [raw], external_document_id=DOC_ID).blocks[0]

    assert block.content == '样式页面E=mc^2'
    assert block.spans[0].style == {
        'bold': True,
        'italic': True,
        'strikethrough': True,
        'underline': True,
        'inline_code': True,
        'background_color': 'red_background',
        'link': {'url': 'https://example.com'},
    }
    assert block.spans[1].style == {
        'text_color': 'blue',
        'notion:rich_text_type': 'mention',
        'notion:mention': {'type': 'page', 'page': {'id': HEADING_ID}},
    }
    assert block.spans[2].style == {
        'notion:rich_text_type': 'equation',
        'notion:equation': {'expression': 'E=mc^2'},
    }


@pytest.mark.parametrize(('block_type', 'payload', 'plain_text', 'expected_type', 'expected_content'), [
    ('code', {'rich_text': [_rich('print(1)')], 'language': 'python'}, '', 'code', 'print(1)'),
    ('image', {'caption': [_rich('图注')]}, '', 'image', '图注'),
    ('divider', {}, '', 'divider', ''),
    ('link_preview', {'url': 'https://example.com'}, '', 'link_preview', 'https://example.com'),
])
def test_maps_special_blocks(block_type, payload, plain_text, expected_type, expected_content):
    raw = _block(PARAGRAPH_ID, block_type, payload)
    raw['plain_text'] = plain_text

    block = NotionWriterAdapter().blocks_to_ir(
        [raw], external_document_id=DOC_ID).blocks[0]

    assert block.type == expected_type
    assert block.content == expected_content
    if block_type == 'code':
        assert block.provider_payload['code_language'] == 'python'


def test_preserves_notion_table_rows_without_virtual_cells():
    table_id = '55555555-5555-5555-5555-555555555555'
    row_id = '66666666-6666-6666-6666-666666666666'
    table = _block(table_id, 'table', {
        'table_width': 2,
        'has_column_header': True,
        'has_row_header': False,
    }, has_children=True)
    linked = _rich('B1')
    linked['href'] = f'https://notion.so/{DOC_ID}#{HEADING_ID.replace("-", "")}'
    row = _block(row_id, 'table_row', {
        'cells': [[_rich('A1', bold=True)], [linked]],
    }, parent=table_id)
    row['plain_text'] = 'A1 | B1'
    target = _block(HEADING_ID, 'heading_1', {'rich_text': [_rich('目标')]})

    document = NotionWriterAdapter().blocks_to_ir(
        [target, table, row], external_document_id=DOC_ID)

    table_block = document.blocks[1]
    row_block = table_block.children[0]
    assert table_block.type == 'table'
    assert row_block.type == 'table_row'
    assert row_block.provider_binding['block_id'] == row_id
    assert row_block.content == 'A1 | B1'
    assert row_block.children == []
    assert row_block.provider_payload['table_cells'][0][0]['annotations']['bold'] is True
    assert row_block.provider_payload['table_cells'][1][0]['href'] == linked['href']


@pytest.mark.parametrize('block_type', [
    'toggle', 'equation', 'bookmark', 'embed', 'child_page', 'child_database',
    'synced_block', 'meeting_notes', 'file', 'video', 'audio', 'pdf',
    'breadcrumb', 'table_of_contents', 'template', 'tab', 'unsupported',
])
def test_unhandled_notion_types_map_to_unknown(block_type):
    raw = _block(PARAGRAPH_ID, block_type, {})
    raw['plain_text'] = f'{block_type} content'

    block = NotionWriterAdapter().blocks_to_ir(
        [raw], external_document_id=DOC_ID).blocks[0]

    assert block.type == 'notion_unknown'
    assert block.content == f'{block_type} content'
    assert block.editable is False
    assert block.provider_payload['raw_block'] == raw


def test_ir_to_blocks_round_trips_text_styles_payload_and_children():
    child = _block(CHILD_ID, 'paragraph', {'rich_text': [_rich('子段落')]}, parent=PARAGRAPH_ID)
    parent = _block(PARAGRAPH_ID, 'to_do', {
        'rich_text': [_rich('任务', bold=True)],
        'checked': True,
        'color': 'yellow_background',
    }, has_children=True)
    document = NotionWriterAdapter().blocks_to_ir(
        [parent, child], external_document_id=DOC_ID,
        uri=f'https://www.notion.so/Page-{DOC_ID.replace("-", "")}')

    blocks = NotionWriterAdapter().ir_to_blocks(document)

    assert blocks == [{
        'object': 'block',
        'type': 'to_do',
        'to_do': {
            'rich_text': [{
                'type': 'text',
                'annotations': {
                    'bold': True, 'italic': False, 'strikethrough': False,
                    'underline': False, 'code': False, 'color': 'default',
                },
                'text': {'content': '任务', 'link': None},
            }],
            'checked': True,
            'color': 'yellow_background',
            'children': [{
                'object': 'block',
                'type': 'paragraph',
                'paragraph': {
                    'rich_text': [{
                        'type': 'text',
                        'annotations': {
                            'bold': False, 'italic': False, 'strikethrough': False,
                            'underline': False, 'code': False, 'color': 'default',
                        },
                        'text': {'content': '子段落', 'link': None},
                        }],
                    },
            }],
        },
    }]


@pytest.mark.parametrize(('image_type', 'file_payload', 'expected'), [
    ('file', {'url': 'https://example.com/image.png'}, True),
    ('external', {'url': 'https://example.com/image.png'}, True),
    ('file', {'expiry_time': '2026-08-26T08:58:31.930Z'}, False),
])
def test_existing_notion_image_payload_reusability(image_type, file_payload, expected):
    raw = _block(PARAGRAPH_ID, 'image', {
        'caption': [_rich('图片说明')],
        'type': image_type,
        image_type: file_payload,
    })
    block = NotionWriterAdapter().blocks_to_ir(
        [raw], external_document_id=DOC_ID).blocks[0]

    assert NotionWriterAdapter.has_reusable_image_payload(block) is expected
    expected_references = []
    if file_payload.get('url'):
        expected_references = [{
            'type': 'preview_asset',
            'provider': 'notion',
            'url': file_payload['url'],
        }]
    assert block.references == expected_references


def test_ir_to_blocks_attaches_local_image_for_supplier_upload(tmp_path):
    image_path = tmp_path / 'image.png'
    image_path.write_bytes(b'png')
    document = WriterDocument(
        document_id='writer-doc',
        provider_binding={'provider': 'notion', 'document_id': DOC_ID},
        blocks=[WriterBlock(
            node_id='new-image',
            type='image',
            content='图片说明',
            references=[{'type': 'media_asset', 'id': 'asset-1'}],
        )],
    )
    media = MediaAssetLibrary(
        library_id='library-1',
        assets={
            'asset-1': MediaAsset(
                media_asset_id='asset-1',
                asset_type='image',
                source_type='input_resource',
                local_path=str(image_path),
            ),
        },
    )

    output = NotionWriterAdapter().ir_to_blocks(document, media_assets=media)

    assert output == [{
        'object': 'block',
        'type': 'image',
        'image': {
            'caption': [{
                'type': 'text',
                'annotations': {
                    'bold': False, 'italic': False, 'strikethrough': False,
                    'underline': False, 'code': False, 'color': 'default',
                },
                'text': {'content': '图片说明', 'link': None},
            }],
        },
        '_media': {
            'media_asset_id': 'asset-1',
            'local_path': str(image_path),
            'file_name': 'image.png',
        },
    }]


@pytest.mark.parametrize('block_type', [
    'paragraph', 'heading_1', 'heading_2', 'heading_3', 'heading_4',
    'bulleted_list_item', 'numbered_list_item', 'to_do', 'quote', 'callout', 'code',
])
def test_update_patch_supports_all_editable_native_text_blocks(block_type):
    adapter = NotionWriterAdapter()
    payload = {'rich_text': [_rich('old')]}
    if block_type == 'code':
        payload['language'] = 'python'
    document = adapter.blocks_to_ir(
        [_block(PARAGRAPH_ID, block_type, payload)], external_document_id=DOC_ID)
    target = document.blocks[0]
    desired = target.model_copy(deep=True)
    desired.content = 'new'
    desired.spans = [WriterSpan(text='new', style={'bold': True})]
    operation = adapter.patch_to_operation(PatchHunk(
        target_node_id=target.node_id, modify_type='update', block=desired), document)

    assert operation.operation == 'update'
    assert operation.params['block_id'] == PARAGRAPH_ID
    assert operation.params['block']['type'] == block_type
    assert operation.params['block'][block_type]['rich_text'][0]['text']['content'] == 'new'
    assert operation.params['block'][block_type]['rich_text'][0]['annotations']['bold'] is True


def test_create_patch_serializes_nested_subtree_with_temporary_ids():
    document = NotionWriterAdapter().blocks_to_ir(
        [_block(PARAGRAPH_ID, 'paragraph', {'rich_text': [_rich('existing')]})],
        external_document_id=DOC_ID,
        uri=f'https://www.notion.so/Page-{DOC_ID.replace("-", "")}',
    )
    created = WriterBlock(
        node_id='new-callout', type='callout', content='New container',
        children=[WriterBlock(
            node_id='new-paragraph', type='paragraph', content='Child',
            spans=[WriterSpan(text='Existing', style={
                'link': {
                    'type': 'internal_ref',
                    'target_node_id': document.blocks[0].node_id,
                },
            })],
        )],
    )

    operation = NotionWriterAdapter().patch_to_operation(PatchHunk(
        target_node_id=created.node_id, modify_type='create',
        block=created, index=1,
    ), document)

    assert operation.operation == 'create'
    assert operation.params['parent_block_id'] == DOC_ID
    assert operation.params['index'] == 1
    assert len(operation.params['blocks']) == 1
    native = operation.params['blocks'][0]
    assert native['_temporary_node_id'] == 'new-callout'
    child = native['callout']['children'][0]
    assert child['_temporary_node_id'] == 'new-paragraph'
    assert child['paragraph']['rich_text'][0]['text']['link']['url'].endswith(
        f'#{PARAGRAPH_ID.replace("-", "")}')


def test_move_patch_clones_bound_subtree_to_requested_position():
    child = _block(CHILD_ID, 'paragraph', {'rich_text': [_rich('child')]},
                   parent=PARAGRAPH_ID)
    source = _block(PARAGRAPH_ID, 'callout', {
        'rich_text': [_rich('source')],
    }, has_children=True)
    sibling = _block(HEADING_ID, 'paragraph', {'rich_text': [_rich('sibling')]})
    document = NotionWriterAdapter().blocks_to_ir(
        [source, child, sibling], external_document_id=DOC_ID)
    source_node = document.blocks[0]

    operation = NotionWriterAdapter().patch_to_operation(PatchHunk(
        target_node_id=source_node.node_id, modify_type='move', index=1,
    ), document)

    assert operation.operation == 'move'
    assert operation.params['source_block_id'] == PARAGRAPH_ID
    assert operation.params['source_parent_block_id'] == DOC_ID
    assert operation.params['source_index'] == 0
    assert operation.params['target_parent_block_id'] == DOC_ID
    assert operation.params['target_index'] == 1
    assert operation.params['block']['_temporary_node_id'] == source_node.node_id
    child_native = operation.params['block']['callout']['children'][0]
    assert child_native['_temporary_node_id'] == source_node.children[0].node_id

