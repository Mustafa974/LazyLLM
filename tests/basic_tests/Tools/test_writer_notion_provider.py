from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from lazyllm.tools.writer.data_models import (
    MediaAsset, MediaAssetLibrary, PatchHunk, PatchSet, TargetDocument,
    WriterBlock, WriterDocument, WriterSpan,
)
from lazyllm.tools.writer.provider import (
    NotionWriterProvider,
    get_writer_provider,
    match_writer_provider,
)
from lazyllm.tools.writer.tools.resource_tools import WriterResourceTools
from lazyllm.tools.writer.utils import load_artifact_json


PAGE_ID = '3c77b0d7-54e0-80d2-b507-d786256d3b18'
BLOCK_ID = '11111111-1111-1111-1111-111111111111'
PAGE_URL = f'https://www.notion.so/Writer-{PAGE_ID.replace("-", "")}'


def _raw_blocks():
    return [{
        'object': 'block',
        'id': BLOCK_ID,
        'block_id': BLOCK_ID,
        'type': 'paragraph',
        'block_type': 'paragraph',
        'parent': {'type': 'page_id', 'page_id': PAGE_ID},
        'parent_id': PAGE_ID,
        'paragraph': {
            'rich_text': [{
                'type': 'text',
                'plain_text': 'Notion content',
                'text': {'content': 'Notion content', 'link': None},
                'annotations': {
                    'bold': False, 'italic': False, 'strikethrough': False,
                    'underline': False, 'code': False, 'color': 'default',
                },
            }],
        },
    }]


def _metadata():
    return {
        'document_id': PAGE_ID,
        'object_type': 'page',
        'title': 'Writer page',
        'browser_url': PAGE_URL,
        'internal_uri': f'notion:/~page/{PAGE_ID}',
        'last_edited_time': '2026-08-28T08:00:00.000Z',
    }


def _make_fs():
    fs = MagicMock()
    fs.get_document_metadata.return_value = _metadata()
    fs.get_doc_blocks.return_value = _raw_blocks()
    fs.replace_doc_blocks.return_value = _raw_blocks()
    fs.write_doc_blocks.return_value = _raw_blocks()
    return fs


@contextmanager
def _route_notion(fs, real_path=None):
    path = real_path or f'/~page/{PAGE_ID}'
    with patch(
        'lazyllm.tools.fs.client.FS._parse',
        return_value=('notion', None, path),
    ), patch(
        'lazyllm.tools.fs.client.FS._get_or_create_fs',
        return_value=fs,
    ):
        yield


def test_notion_provider_is_registered_and_matches_supported_locators():
    assert isinstance(get_writer_provider('notion'), NotionWriterProvider)
    assert isinstance(match_writer_provider(PAGE_URL), NotionWriterProvider)
    assert isinstance(
        match_writer_provider(f'notion:/~page/{PAGE_ID}'),
        NotionWriterProvider,
    )
    assert NotionWriterProvider().resolve(PAGE_URL) == TargetDocument(
        uri=PAGE_URL,
        adapter='notion',
    )


def test_notion_provider_loads_ir_with_page_metadata(tmp_path):
    fs = _make_fs()
    with _route_notion(fs):
        result = WriterResourceTools(
            artifact_store=str(tmp_path),
        ).load_document({'uri': PAGE_URL, 'adapter': 'notion'})

    source = load_artifact_json(
        result['metadata']['artifact_paths']['source_document'],
        WriterDocument,
    )
    target = load_artifact_json(
        result['metadata']['artifact_paths']['target_document'],
        TargetDocument,
    )
    assert result['representation'] == 'ir'
    assert source.title == 'Writer page'
    assert source.revision == '2026-08-28T08:00:00.000Z'
    assert source.provider_binding['provider'] == 'notion'
    assert source.provider_binding['document_id'] == PAGE_ID
    assert target.doc_id == PAGE_ID
    assert target.meta['internal_uri'] == f'notion:/~page/{PAGE_ID}'
    fs.get_doc_blocks.assert_called_once_with(f'/~page/{PAGE_ID}', with_descendants=True)


def test_notion_provider_replaces_writer_ir_without_markdown_fallback(tmp_path):
    fs = _make_fs()
    provider = NotionWriterProvider()
    with _route_notion(fs):
        loaded = provider.load_document(
            TargetDocument(uri=PAGE_URL, adapter='notion'),
        )
        document = loaded['source_document']
        result = provider.replace_document(
            document,
            loaded['target_document'],
        )

    assert result == {
        'doc_id': PAGE_ID,
        'adapter': 'notion',
        'locator': PAGE_URL,
        'block_count': 1,
        'warnings': [],
    }
    fs.update_page_title.assert_called_once_with(PAGE_ID, 'Writer page')
    fs.replace_doc_blocks.assert_called_once()
    assert fs.replace_doc_blocks.call_args.args[0] == PAGE_ID
    assert fs.replace_doc_blocks.call_args.args[1][0]['type'] == 'paragraph'


def test_notion_provider_converts_markdown_to_ir_before_writing():
    pytest.importorskip('mistune')
    fs = _make_fs()
    with _route_notion(fs):
        result = NotionWriterProvider().replace_document(
            '# Safety guide\n\n## Before\n\nPrepare supplies.',
            TargetDocument(uri=PAGE_URL, adapter='notion'),
        )

    assert result['doc_id'] == PAGE_ID
    native = fs.replace_doc_blocks.call_args.args[1]
    # The Markdown H1 becomes the document title; its H2 section becomes the
    # first provider heading and the body remains a paragraph.
    assert [block['type'] for block in native] == ['heading_1', 'paragraph']
    assert native[0]['heading_1']['rich_text'][0]['text']['content'] == '1. Before'
    fs.update_page_title.assert_called_once_with(PAGE_ID, 'Safety guide')


def test_notion_provider_materializes_heading_and_figure_numbers_on_write(tmp_path):
    image_path = tmp_path / 'figure.png'
    image_path.write_bytes(b'fake image content')
    media_assets = MediaAssetLibrary(
        library_id='numbering-media',
        assets={
            'asset-1': MediaAsset(
                media_asset_id='asset-1', asset_type='generated_image',
                source_type='image_generation', local_path=str(image_path),
            ),
            'asset-2': MediaAsset(
                media_asset_id='asset-2', asset_type='generated_image',
                source_type='image_generation', local_path=str(image_path),
            ),
        },
    )
    document = WriterDocument(
        document_id='writer-numbering', stage='final', title='Numbered document',
        provider_binding={'provider': 'notion', 'document_id': PAGE_ID, 'uri': PAGE_URL},
        blocks=[
            WriterBlock(
                node_id='section-1', type='heading', content='Preparation',
                numbering={'level': 1},
                children=[
                    WriterBlock(
                        node_id='section-1-1', type='heading', content='Supplies',
                        numbering={'level': 2},
                        children=[WriterBlock(
                            node_id='figure-1', type='image', content='Emergency supplies',
                            references=[{'type': 'media_asset', 'id': 'asset-1'}],
                        )],
                    ),
                ],
            ),
            WriterBlock(
                node_id='section-2', type='heading', content='Recovery',
                numbering={'level': 1},
                children=[WriterBlock(
                    node_id='figure-2', type='image', content='Safety checklist',
                    references=[{'type': 'media_asset', 'id': 'asset-2'}],
                )],
            ),
        ],
    )
    fs = _make_fs()

    with _route_notion(fs):
        NotionWriterProvider().replace_document(
            document, TargetDocument(uri=PAGE_URL, adapter='notion'),
            media_assets=media_assets,
        )

    native = fs.replace_doc_blocks.call_args.args[1]
    assert [block['type'] for block in native] == [
        'heading_1', 'heading_2', 'image', 'heading_1', 'image',
    ]
    assert native[0]['heading_1']['rich_text'][0]['text']['content'] == '1. Preparation'
    assert native[1]['heading_2']['rich_text'][0]['text']['content'] == '1.1. Supplies'
    assert native[2]['image']['caption'][0]['text']['content'] == '图1 Emergency supplies'
    assert native[3]['heading_1']['rich_text'][0]['text']['content'] == '2. Recovery'
    assert native[4]['image']['caption'][0]['text']['content'] == '图2 Safety checklist'
    # Publishing uses a materialized copy and does not pollute the canonical IR.
    assert document.blocks[0].content == 'Preparation'
    assert document.blocks[0].children[0].children[0].content == 'Emergency supplies'


def test_notion_numbering_sync_updates_supported_blocks_and_skips_table():
    heading = WriterBlock(
        node_id='heading-1', type='heading', content='台风期间', stage='final',
        numbering={'level': 1}, editable=True,
        provider_payload={'raw_block': {
            'type': 'heading_1',
            'heading_1': {'rich_text': [{
                'type': 'text', 'text': {'content': '台风期间'},
            }]},
        }},
    )
    image = WriterBlock(
        node_id='image-1', type='image', content='安全示意图', stage='final',
        editable=False,
        provider_payload={'raw_block': {
            'type': 'image',
            'image': {'caption': [{
                'type': 'text', 'text': {'content': '安全示意图'},
            }]},
        }},
    )
    code = WriterBlock(
        node_id='code-1', type='code', content='print("safe")', stage='final',
        editable=True,
        provider_payload={'raw_block': {
            'type': 'code',
            'code': {
                'rich_text': [{'type': 'text', 'text': {'content': 'print("safe")'}}],
                'caption': [],
                'language': 'python',
            },
        }},
    )
    table = WriterBlock(
        node_id='table-1', type='table', content='Safety table', stage='final',
        editable=False,
        provider_payload={'raw_block': {'type': 'table', 'table': {}}},
    )
    persisted = WriterDocument(
        document_id='writer-doc', stage='final', blocks=[heading, image, code, table])
    numbered = persisted.model_copy(deep=True)
    numbered.blocks[0].content = '1. 台风期间'
    numbered.blocks[1].content = '图1 安全示意图'
    numbered.blocks[2].provider_payload['numbering_caption'] = '代码1'
    numbered.blocks[3].content = '表1 Safety table'

    hunks = NotionWriterProvider._numbering_sync_hunks(numbered, persisted)

    assert [hunk.hunk_id for hunk in hunks] == [
        'heading-numbering-sync-heading-1',
        'image-numbering-sync-image-1',
        'code-numbering-sync-code-1',
    ]
    assert hunks[1].meta == {
        'source': 'system_numbering', 'update_scope': 'caption',
    }
    assert hunks[2].meta == {
        'source': 'system_numbering', 'update_scope': 'caption',
    }


def test_notion_provider_creates_private_workspace_page_without_parent():
    fs = _make_fs()
    fs.create_document.return_value = {
        'document_id': PAGE_ID,
        'title': 'Private page',
        'browser_url': PAGE_URL,
        'internal_uri': f'notion:/~page/{PAGE_ID}',
        'last_edited_time': '2026-08-28T08:00:00.000Z',
    }
    with _route_notion(fs, real_path='/'):
        target = NotionWriterProvider().create_document('Private page')

    assert target.adapter == 'notion'
    assert target.doc_id == PAGE_ID
    assert target.uri == PAGE_URL
    assert target.meta['parent_uri'] == ''
    fs.create_document.assert_called_once_with('Private page', '/')


def test_notion_provider_applies_update_and_refreshes_persisted_document():
    fs = _make_fs()
    updated = _raw_blocks()
    updated[0]['paragraph']['rich_text'][0]['plain_text'] = 'Updated'
    updated[0]['paragraph']['rich_text'][0]['text']['content'] = 'Updated'
    fs.get_doc_blocks.side_effect = [_raw_blocks(), updated]
    fs.get_document_metadata.side_effect = [
        _metadata(), _metadata(), _metadata(), _metadata(),
        {**_metadata(), 'last_edited_time': '2026-08-28T08:01:00.000Z'},
    ]
    provider = NotionWriterProvider()
    with _route_notion(fs):
        loaded = provider.load_document(TargetDocument(uri=PAGE_URL, adapter='notion'))
        source = loaded['source_document']
        desired = source.blocks[0].model_copy(update={
            'content': 'Updated', 'spans': [WriterSpan(text='Updated')],
        })
        result = provider.apply_patch_to_document(PatchSet(
            target_doc_id=source.document_id,
            hunks=[PatchHunk(
                hunk_id='update-paragraph', target_node_id=desired.node_id,
                modify_type='update', block=desired,
            )],
        ), source, loaded['target_document'])

    fs.update_block.assert_called_once()
    assert result['patch_result'].success is True
    assert result['patch_result'].applied_hunks == ['update-paragraph']
    assert result['persisted_document'].blocks[0].content == 'Updated'
    assert result['persisted_document'].blocks[0].node_id == source.blocks[0].node_id
    assert result['persisted_document'].revision == '2026-08-28T08:01:00.000Z'


def test_notion_provider_creates_block_and_rebinds_writer_node_id():
    fs = _make_fs()
    created_id = '22222222-2222-2222-2222-222222222222'
    created_raw = {
        'object': 'block',
        'id': created_id,
        'block_id': created_id,
        'type': 'paragraph',
        'block_type': 'paragraph',
        'parent': {'type': 'page_id', 'page_id': PAGE_ID},
        'parent_id': PAGE_ID,
        'paragraph': {
            'rich_text': [{
                'type': 'text',
                'plain_text': 'Created',
                'text': {'content': 'Created', 'link': None},
                'annotations': {
                    'bold': False, 'italic': False, 'strikethrough': False,
                    'underline': False, 'code': False, 'color': 'default',
                },
            }],
        },
    }
    fs.get_doc_blocks.side_effect = [_raw_blocks(), [*_raw_blocks(), created_raw]]
    fs.get_document_metadata.side_effect = [
        _metadata(), _metadata(), _metadata(), _metadata(),
        {**_metadata(), 'last_edited_time': '2026-08-28T08:01:00.000Z'},
    ]
    fs.create_block.return_value = {
        'block_id': created_id,
        'block_id_relations': [{
            'temporary_block_id': 'new-paragraph', 'block_id': created_id,
        }],
    }
    provider = NotionWriterProvider()
    with _route_notion(fs):
        loaded = provider.load_document(TargetDocument(uri=PAGE_URL, adapter='notion'))
        source = loaded['source_document']
        created = WriterBlock(
            node_id='new-paragraph', type='paragraph', content='Created')
        result = provider.apply_patch_to_document(PatchSet(
            target_doc_id=source.document_id,
            hunks=[PatchHunk(
                hunk_id='create-paragraph', target_node_id=created.node_id,
                modify_type='create', block=created, index=1,
            )],
        ), source, loaded['target_document'])

    fs.create_block.assert_called_once()
    call = fs.create_block.call_args.kwargs
    assert call['document_id'] == PAGE_ID
    assert call['parent_block_id'] == PAGE_ID
    assert call['index'] == 1
    assert len(call['blocks']) == 1
    assert call['blocks'][0]['_temporary_node_id'] == 'new-paragraph'
    assert result['patch_result'].applied_hunks == ['create-paragraph']
    assert result['persisted_document'].blocks[1].node_id == 'new-paragraph'
    assert result['persisted_document'].blocks[1].content == 'Created'


def test_notion_provider_moves_block_and_preserves_writer_node_id():
    fs = _make_fs()
    second_id = '22222222-2222-2222-2222-222222222222'
    moved_id = '33333333-3333-3333-3333-333333333333'
    second = deepcopy(_raw_blocks()[0])
    second['id'] = second['block_id'] = second_id
    second['paragraph']['rich_text'][0]['plain_text'] = 'Second'
    second['paragraph']['rich_text'][0]['text']['content'] = 'Second'
    moved = deepcopy(_raw_blocks()[0])
    moved['id'] = moved['block_id'] = moved_id
    fs.get_doc_blocks.side_effect = [[*_raw_blocks(), second], [second, moved]]
    fs.get_document_metadata.side_effect = [
        _metadata(), _metadata(), _metadata(), _metadata(),
        {**_metadata(), 'last_edited_time': '2026-08-28T08:01:00.000Z'},
    ]
    provider = NotionWriterProvider()
    fs.move_block.return_value = {
        'block_id': moved_id,
        'source_block_id': BLOCK_ID,
        'block_id_relations': [{
            'temporary_block_id': '', 'block_id': moved_id,
        }],
    }
    with _route_notion(fs):
        loaded = provider.load_document(TargetDocument(uri=PAGE_URL, adapter='notion'))
        source = loaded['source_document']
        moved_node_id = source.blocks[0].node_id
        fs.move_block.return_value['block_id_relations'][0]['temporary_block_id'] = moved_node_id
        result = provider.apply_patch_to_document(PatchSet(
            target_doc_id=source.document_id,
            hunks=[PatchHunk(
                hunk_id='move-paragraph', target_node_id=moved_node_id,
                modify_type='move', index=1,
            )],
        ), source, loaded['target_document'])

    fs.move_block.assert_called_once()
    assert result['patch_result'].applied_hunks == ['move-paragraph']
    assert [block.content for block in result['persisted_document'].blocks] == [
        'Second', 'Notion content',
    ]
    assert result['persisted_document'].blocks[1].node_id == moved_node_id
