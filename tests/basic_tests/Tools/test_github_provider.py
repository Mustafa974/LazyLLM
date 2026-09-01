import hashlib
from unittest.mock import MagicMock, patch

from lazyllm.tools.writer.data_models.multimodal import MediaAsset, MediaAssetLibrary
from lazyllm.tools.writer.data_models.task import TargetDocument
from lazyllm.tools.writer.provider import get_writer_provider, match_writer_provider
from lazyllm.tools.writer.provider.github import GitHubWriterProvider


def _resolved_target():
    return {
        'doc_id': 'acme/docs:main:guide.md',
        'uri': 'githubrepo:/acme/docs/guide.md?ref=main',
        'browser_url': 'https://github.com/acme/docs/blob/main/guide.md',
        'title': 'guide',
        'owner': 'acme',
        'repo': 'docs',
        'ref': 'main',
        'path': 'guide.md',
        'revision': 'commit-1',
        'blob_sha': 'blob-1',
        'target_type': 'repository',
        'fs_scheme': 'githubrepo',
    }


def test_github_provider_is_registered_once_for_repo_and_wiki():
    assert get_writer_provider('github').provider == 'github'
    assert match_writer_provider(
        'https://github.com/acme/docs/blob/main/guide.md'
    ).provider == 'github'
    assert match_writer_provider(
        'https://github.com/acme/docs/wiki/Guide'
    ).provider == 'github'


def test_load_document_preserves_markdown_and_collects_direct_resources():
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [
        b'# Guide\n\n![diagram](assets/diagram.png)\n\n[Next](next.md)\n',
        b'png-bytes',
    ]
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri='https://github.com/acme/docs/blob/main/guide.md',
            adapter='github',
            meta={'target_type': 'repository'},
        ))

    assert loaded['representation'] == 'markdown'
    assert loaded['source_document'].startswith('# Guide')
    assert len(loaded['input_resources']) == 1
    assert loaded['input_resources'][0].resource_type == 'image'
    assert loaded['input_resources'][0].uri == (
        'githubrepo:/acme/docs/assets/diagram.png?ref=main'
    )
    assert loaded['input_resources'][0].meta['source_reference'] == 'assets/diagram.png'


def test_load_document_keeps_pr_continuation_metadata():
    fs = MagicMock()
    resolved = _resolved_target()
    resolved.update({
        'uri': 'githubrepo:/acme/docs/guide.md?ref=lazymind%2Fop',
        'ref': 'lazymind/op',
        'revision': 'commit-2',
    })
    fs.resolve_target.return_value = resolved
    fs.read_bytes.return_value = b'# Guide\n'
    provider = GitHubWriterProvider()
    target = TargetDocument(
        uri=resolved['uri'],
        adapter='github',
        meta={
            'target_type': 'repository',
            'work_branch': 'lazymind/op',
            'base_ref': 'main',
            'pull_request_url': 'https://github.com/acme/docs/pull/9',
        },
    )

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(target)

    refreshed = loaded['target_document']
    assert refreshed.meta['work_branch'] == 'lazymind/op'
    assert refreshed.meta['base_ref'] == 'main'
    assert refreshed.meta['revision'] == 'commit-2'


def test_replace_document_passes_final_markdown_and_provider_result():
    fs = MagicMock()
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'target_type': 'repository',
        'doc_id': 'acme/docs:work:guide.md',
        'uri': 'githubrepo:/acme/docs/guide.md?ref=lazymind%2Fop',
        'browser_url': 'https://github.com/acme/docs/pull/9',
        'publish_mode': 'pull_request',
        'commit_sha': 'commit-2',
        'revision': 'commit-2',
        'work_branch': 'lazymind/op',
        'pull_request_url': 'https://github.com/acme/docs/pull/9',
        'warnings': [],
    }
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    with patch.object(provider, '_fs', return_value=fs):
        result = provider.replace_document('# Final\n', target)

    assert fs.apply_document_patch.call_args.args[1] == '# Final\n'
    assert result['commit_sha'] == 'commit-2'
    assert result['adapter'] == 'github'
    assert target.meta['work_branch'] == 'lazymind/op'
    assert target.uri.endswith('ref=lazymind%2Fop')


def test_replace_document_does_not_reuse_completed_operation_id():
    fs = MagicMock()
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'target_type': 'repository',
        'doc_id': 'acme/docs:work:guide.md',
        'uri': 'githubrepo:/acme/docs/guide.md?ref=lazymind%2Fop',
        'revision': 'commit-3',
        'commit_sha': 'commit-3',
        'work_branch': 'lazymind/op',
        'operation_id': 'new-operation',
        'warnings': [],
    }
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())
    target.meta.update({
        'operation_id': 'completed-operation',
        'last_operation_id': 'completed-operation',
        'work_branch': 'lazymind/op',
        'base_ref': 'main',
    })

    with patch.object(provider, '_fs', return_value=fs):
        provider.replace_document('# Changed again\n', target)

    assert fs.apply_document_patch.call_args.kwargs['operation_id'] == ''
    assert target.meta['last_operation_id'] == 'new-operation'


def test_replace_document_materializes_referenced_asset_with_relative_link(tmp_path):
    image = tmp_path / 'diagram.svg'
    image_data = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    image.write_bytes(image_data)
    library = MediaAssetLibrary(
        library_id='library-1',
        assets={
            'asset-1': MediaAsset(
                media_asset_id='asset-1',
                asset_type='image',
                source_type='input_resource',
                local_path=str(image),
            ),
        },
    )
    resolved = _resolved_target()
    resolved['path'] = 'docs/guide.md'
    target = GitHubWriterProvider._target_from_resolved(resolved)

    markdown, files = GitHubWriterProvider()._materialize_media(
        '![diagram](asset://asset-1)', target, library,
    )

    digest = hashlib.sha256(image_data).hexdigest()
    repository_path = f'docs/assets/{digest[:2]}/{digest}.svg'
    assert markdown == f'![diagram](assets/{digest[:2]}/{digest}.svg)'
    assert files == {repository_path: image_data}


def test_replace_document_restores_imported_image_reference(tmp_path):
    image = tmp_path / 'diagram.png'
    image.write_bytes(b'preview-only')
    library = MediaAssetLibrary(
        library_id='library-1',
        assets={
            'asset-1': MediaAsset(
                media_asset_id='asset-1',
                asset_type='image',
                source_type='input_resource',
                local_path=str(image),
                meta={
                    'source_reference': './diagram.png',
                    'preview_reference': (
                        '/static-files/writer-preview-assets/aa/diagram.png'
                        '?expires=123&sig=abc'
                    ),
                },
            ),
        },
    )
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    markdown, files = GitHubWriterProvider()._materialize_media(
        '![diagram](/static-files/writer-preview-assets/aa/diagram.png'
        '?expires=123&sig=abc)',
        target,
        library,
    )

    assert markdown == '![diagram](./diagram.png)'
    assert files == {}


def test_github_provider_rejects_non_markdown_repo_urls():
    assert not GitHubWriterProvider.matches(
        'https://github.com/acme/docs/blob/main/source.py'
    )
