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


def _write_result(**updates):
    result = {
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
    result.update(updates)
    return result


def test_github_provider_matches_repository_and_wiki_markdown():
    assert get_writer_provider('github').provider == 'github'
    assert match_writer_provider(
        'https://github.com/acme/docs/blob/main/guide.md'
    ).provider == 'github'
    assert match_writer_provider(
        'https://github.com/acme/docs/wiki/Guide'
    ).provider == 'github'
    assert not GitHubWriterProvider.matches(
        'https://github.com/acme/docs/blob/main/source.py'
    )


def test_load_document_imports_only_images_and_keeps_main_document_on_failure():
    raw_url = (
        'https://raw.githubusercontent.com/LazyAGI/LazyLLM/'
        'main/docs/assets/LazyLLM-logo.png'
    )
    markdown = (
        '# Guide\n\n'
        '[License](LICENSE)\n\n'
        '![diagram](assets/diagram.png)\n\n'
        f'![logo]({raw_url})\n\n'
        '![missing](assets/missing.png)\n\n'
        '<video src="assets/demo.mp4"></video>\n'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [
        markdown.encode(),
        b'diagram',
        b'logo',
        FileNotFoundError('missing image'),
    ]
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri=_resolved_target()['uri'],
            adapter='github',
            meta={'target_type': 'repository'},
        ))

    assert loaded['source_document'] == markdown
    assert [item.meta['source_reference'] for item in loaded['input_resources']] == [
        'assets/diagram.png',
        raw_url,
    ]
    assert loaded['resource_warnings'] == [
        'assets/missing.png: FileNotFoundError',
    ]


def test_imported_html_image_layout_is_restored_before_writeback():
    layout = (
        '<table><tr><td><a href="docs/assets/artifact.jpg">'
        '<img src="docs/assets/artifact.jpg" alt="Artifact" width="100%" /></a>'
        '<br/><sub>Original caption</sub></td></tr></table>'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [f'# Original\n\n{layout}\n'.encode(), b'image']
    fs.apply_document_patch.return_value = _write_result()
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri=_resolved_target()['uri'],
            adapter='github',
            meta={'target_type': 'repository'},
        ))
        preview = '/static-files/writer-preview-assets/aa/artifact.jpg?sig=x'
        library = MediaAssetLibrary(
            library_id='library-1',
            assets={
                'asset-1': MediaAsset(
                    media_asset_id='asset-1',
                    asset_type='image',
                    source_type='input_resource',
                    meta={
                        'source_reference': 'docs/assets/artifact.jpg',
                        'preview_reference': preview,
                    },
                ),
            },
        )
        edited = loaded['source_document'].replace(
            '# Original', '# Changed',
        ).replace('docs/assets/artifact.jpg', preview)
        provider.replace_document(
            edited,
            TargetDocument.model_validate(loaded['target_document']),
            media_assets=library,
        )

    assert fs.apply_document_patch.call_args.args[1] == f'# Changed\n\n{layout}\n'


def test_unknown_code_fence_round_trips_without_changing_other_content():
    markdown = (
        '# Original\n\n'
        '```bash\necho supported\n```\n\n'
        '```mermaid\nA --> B\n```\n'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.return_value = markdown.encode()
    fs.apply_document_patch.return_value = _write_result()
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri=_resolved_target()['uri'],
            adapter='github',
            meta={'target_type': 'repository'},
        ))
        writer_markdown = loaded['source_document']
        provider.replace_document(
            writer_markdown.replace('# Original', '# Changed'),
            loaded['target_document'],
        )

    assert '```bash\necho supported' in writer_markdown
    assert '```text\nA --> B' in writer_markdown
    assert fs.apply_document_patch.call_args.args[1] == markdown.replace(
        '# Original', '# Changed',
    )


def test_replace_document_writes_final_markdown_and_updates_target_state():
    fs = MagicMock()
    fs.apply_document_patch.return_value = _write_result()
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    with patch.object(provider, '_fs', return_value=fs):
        result = provider.replace_document('# Final\n', target)

    assert fs.apply_document_patch.call_args.args[1] == '# Final\n'
    assert result['commit_sha'] == 'commit-2'
    assert target.meta['work_branch'] == 'lazymind/op'
    assert target.uri.endswith('ref=lazymind%2Fop')


def test_plan_and_create_document_writes_final_file_once():
    fs = MagicMock()
    fs.resolve_create_parent.return_value = {
        'uri': 'https://github.com/acme/docs/tree/main/articles',
        'browser_url': 'https://github.com/acme/docs/tree/main/articles',
        'owner': 'acme',
        'repo': 'docs',
        'ref': 'main',
        'base_ref': 'main',
        'directory': 'articles',
        'revision': 'commit-1',
        'target_type': 'repository',
        'create_pending': True,
    }
    fs.resolve_create_target.return_value = {
        **fs.resolve_create_parent.return_value,
        'doc_id': 'acme/docs:main:articles/新文章.md',
        'uri': 'githubrepo:/acme/docs/articles/new.md?ref=main',
        'title': '新文章',
        'path': 'articles/新文章.md',
        'publish_mode': 'pull_request',
        'create_pending': False,
    }
    fs.apply_document_patch.return_value = _write_result(
        doc_id='acme/docs:work:articles/新文章.md',
        uri='githubrepo:/acme/docs/articles/new.md?ref=lazymind%2Fop',
    )
    provider = GitHubWriterProvider()

    with patch('lazyllm.tools.writer.provider.github.GitHubRepoFS') as fs_type:
        fs_type.matches_create_parent.return_value = True
        fs_type.return_value = fs
        target = provider.plan_document(
            'https://github.com/acme/docs/tree/main/articles',
        )
        provider.replace_document('# 新文章\n\n最终正文。\n', target)

    fs.resolve_create_parent.assert_called_once()
    fs.resolve_create_target.assert_called_once()
    assert fs.apply_document_patch.call_count == 1
    assert target.meta['create_pending'] is False
    assert target.meta['work_branch'] == 'lazymind/op'


def test_plan_and_create_wiki_document_uses_direct_commit():
    fs = MagicMock()
    fs.resolve_create_parent.return_value = {
        'uri': 'https://github.com/acme/docs/wiki',
        'browser_url': 'https://github.com/acme/docs/wiki',
        'owner': 'acme',
        'repo': 'docs',
        'ref': 'master',
        'base_ref': 'master',
        'revision': 'commit-1',
        'target_type': 'wiki',
        'publish_mode': 'direct',
        'create_pending': True,
    }
    fs.resolve_create_target.return_value = {
        **fs.resolve_create_parent.return_value,
        'doc_id': 'acme/docs.wiki:New-Page.md',
        'uri': 'githubwiki:/acme/docs/New-Page.md',
        'browser_url': 'https://github.com/acme/docs/wiki/New-Page',
        'title': 'New-Page',
        'path': 'New-Page.md',
        'create_pending': False,
    }
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'target_type': 'wiki',
        'uri': 'githubwiki:/acme/docs/New-Page.md',
        'revision': 'commit-2',
        'commit_sha': 'commit-2',
        'publish_mode': 'direct',
        'warnings': [],
    }
    provider = GitHubWriterProvider()

    with (
        patch('lazyllm.tools.writer.provider.github.GitHubRepoFS') as repo_type,
        patch('lazyllm.tools.writer.provider.github.GitHubWikiFS') as wiki_type,
    ):
        repo_type.matches_create_parent.return_value = False
        wiki_type.matches_create_parent.return_value = True
        wiki_type.return_value = fs
        target = provider.plan_document('https://github.com/acme/docs/wiki')
        provider.replace_document('# New Page\n\nFinal body.\n', target)

    assert target.meta['target_type'] == 'wiki'
    assert target.meta['publish_mode'] == 'direct'
    fs.resolve_create_parent.assert_called_once_with('https://github.com/acme/docs/wiki')
    assert fs.apply_document_patch.call_args.kwargs['publish_mode'] == 'direct'


def test_generated_image_is_uploaded_with_markdown_in_same_patch(tmp_path):
    image = tmp_path / 'generated.png'
    image_data = b'\x89PNG\r\n\x1a\nwriter-generated-image'
    image.write_bytes(image_data)
    digest = hashlib.sha256(image_data).hexdigest()
    preview = (
        f'/static-files/writer-preview-assets/{digest[:2]}/{digest}.png'
        '?expires=123&sig=abc'
    )
    library = MediaAssetLibrary(
        library_id='library-1',
        assets={
            'generated-1': MediaAsset(
                media_asset_id='generated-1',
                asset_type='generated_image',
                source_type='image_generation',
                local_path=str(image),
            ),
        },
    )
    fs = MagicMock()
    fs.apply_document_patch.return_value = _write_result()
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    with patch.object(provider, '_fs', return_value=fs):
        provider.replace_document(
            f'# Report\n\n![chart]({preview})\n',
            target,
            media_assets=library,
        )

    reference = f'assets/{digest[:2]}/{digest}.png'
    assert fs.apply_document_patch.call_args.args[1] == (
        f'# Report\n\n![chart]({reference})\n'
    )
    assert fs.apply_document_patch.call_args.kwargs['files'] == {
        reference: image_data,
    }
