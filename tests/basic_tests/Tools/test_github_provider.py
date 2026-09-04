import hashlib
from unittest.mock import MagicMock, patch

import pytest

from lazyllm.tools.fs.supplier.github import GitHubFSError
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


def test_load_document_collects_only_images_and_keeps_other_links_untouched():
    markdown = (
        '# Guide\n\n'
        '[License](LICENSE)\n\n'
        '[Attachment](assets/guide.pdf)\n\n'
        '![diagram](assets/diagram.png)\n\n'
        '<img src="assets/logo.svg" alt="logo">\n\n'
        '<video src="assets/demo.mp4"></video>\n\n'
        '<audio src="assets/demo.mp3"></audio>\n\n'
        '<source src="assets/demo.webm">\n'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [
        markdown.encode(),
        b'png-bytes',
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    ]
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri='https://github.com/acme/docs/blob/main/guide.md',
            adapter='github',
            meta={'target_type': 'repository'},
        ))

    assert loaded['source_document'] == markdown
    assert [resource.meta['source_reference'] for resource in loaded['input_resources']] == [
        'assets/diagram.png',
        'assets/logo.svg',
    ]
    assert all(resource.resource_type == 'image' for resource in loaded['input_resources'])
    assert [call.args[0] for call in fs.read_bytes.call_args_list[1:]] == [
        'githubrepo:/acme/docs/assets/diagram.png?ref=main',
        'githubrepo:/acme/docs/assets/logo.svg?ref=main',
    ]


def test_load_document_collects_raw_github_image_without_rewriting_source():
    raw_url = (
        'https://raw.githubusercontent.com/LazyAGI/LazyLLM/'
        'main/docs/assets/LazyLLM-logo.png'
    )
    markdown = f'# Guide\n\n![logo]({raw_url})\n'
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [markdown.encode(), b'png-bytes']
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri='https://github.com/acme/docs/blob/main/guide.md',
            adapter='github',
            meta={'target_type': 'repository'},
        ))

    assert loaded['source_document'] == markdown
    assert len(loaded['input_resources']) == 1
    resource = loaded['input_resources'][0]
    assert resource.uri == (
        'githubrepo:/LazyAGI/LazyLLM/docs/assets/LazyLLM-logo.png?ref=main'
    )
    assert resource.meta['source_reference'] == raw_url


def test_github_html_image_layout_is_renderable_and_restored_before_writeback():
    markdown = (
        '# Guide\n\n'
        '[![Stars](https://img.shields.io/github/stars/acme/docs)]'
        '(https://github.com/acme/docs/stargazers)\n\n'
        '<table><tr>\n'
        '<td><a href="docs/assets/workspace.jpg">'
        '<img src="docs/assets/workspace.jpg" alt="Workspace"></a>'
        '<br><sub>Workspace preview</sub></td>\n'
        '<td><img src="docs/assets/diff.jpg" alt="Diff">'
        '<br><sub>Diff preview</sub></td>\n'
        '</tr></table>\n'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [markdown.encode(), b'workspace', b'diff']
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'target_type': 'repository',
        'doc_id': 'acme/docs:work:guide.md',
        'uri': 'githubrepo:/acme/docs/guide.md?ref=lazymind%2Fop',
        'revision': 'commit-2',
        'commit_sha': 'commit-2',
        'work_branch': 'lazymind/op',
        'warnings': [],
    }
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri='https://github.com/acme/docs/blob/main/guide.md',
            adapter='github',
            meta={'target_type': 'repository'},
        ))
        writer_markdown = loaded['source_document']
        target = loaded['target_document']
        provider.replace_document(writer_markdown.replace('# Guide', '# Updated'), target)

    assert '<table>' not in writer_markdown
    assert '<!--' not in writer_markdown
    assert '![Stars](https://img.shields.io/github/stars/acme/docs)' in writer_markdown
    assert '(https://github.com/acme/docs/stargazers)' not in writer_markdown
    assert '![Workspace](docs/assets/workspace.jpg)' in writer_markdown
    assert '![Diff](docs/assets/diff.jpg)' in writer_markdown
    assert '_Workspace preview_' in writer_markdown
    assert 'github_writer_image_layouts' in target.meta
    assert fs.apply_document_patch.call_args.args[1] == markdown.replace(
        '# Guide', '# Updated',
    )


def test_load_document_keeps_main_document_when_one_image_fails():
    markdown = (
        '# Guide\n\n'
        '![missing](assets/missing.png)\n\n'
        '![logo](assets/logo.svg)\n'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [
        markdown.encode(),
        FileNotFoundError('missing image'),
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    ]
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri='https://github.com/acme/docs/blob/main/guide.md',
            adapter='github',
            meta={'target_type': 'repository'},
        ))

    assert loaded['source_document'] == markdown
    assert [resource.meta['source_reference'] for resource in loaded['input_resources']] == [
        'assets/logo.svg',
    ]
    assert loaded['resource_warnings'] == [
        'assets/missing.png: FileNotFoundError',
    ]


def test_unknown_code_fences_are_plain_text_in_writer_and_restored_on_writeback():
    markdown = (
        '# Original\n\n'
        '```bash\necho supported\n```\n\n'
        '```mermaid\nflowchart LR\nA --> B\n```\n\n'
        '```go\npackage main\n```\n\n'
        '```java\nclass Main {}\n```\n'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.return_value = markdown.encode()
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'uri': _resolved_target()['uri'],
        'revision': 'commit-2',
        'warnings': [],
    }
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri=_resolved_target()['uri'],
            adapter='github',
            meta={'target_type': 'repository'},
        ))
        writer_markdown = loaded['source_document']
        target = loaded['target_document']
        provider.replace_document(
            writer_markdown.replace('# Original', '# Changed'), target,
        )

    assert '```bash\necho supported' in writer_markdown
    assert '```mermaid' not in writer_markdown
    assert '```go' not in writer_markdown
    assert '```java' not in writer_markdown
    assert writer_markdown.count('```text') == 3
    assert [
        item['language'] for item in target.meta['github_writer_code_fences']
    ] == ['mermaid', 'go', 'java']
    assert fs.apply_document_patch.call_args.args[1] == markdown.replace(
        '# Original', '# Changed',
    )


def test_changed_unknown_code_fence_is_not_restored_as_original():
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())
    normalized = provider.normalize_code_fences_for_writer(
        '```mermaid\nA --> B\n```\n', target,
    )

    changed = normalized.replace('A --> B', 'A --> C')

    assert provider._restore_code_fences(changed, target) == changed


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


def test_resolve_create_target_validates_without_creating_remote_content():
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
    provider = GitHubWriterProvider()

    with patch('lazyllm.tools.writer.provider.github.GitHubRepoFS') as fs_type:
        fs_type.matches_create_parent.return_value = True
        fs_type.return_value = fs
        target = provider._resolve_create_target(
            'https://github.com/acme/docs/tree/main/articles',
        )

    assert target.adapter == 'github'
    assert target.meta['create_pending'] is True
    fs.resolve_create_parent.assert_called_once()
    fs.apply_document_patch.assert_not_called()


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
        target = provider._resolve_create_target('https://github.com/acme/docs/wiki')
        provider.replace_document('# New Page\n\nFinal body.\n', target)

    assert target.meta['target_type'] == 'wiki'
    assert target.meta['publish_mode'] == 'direct'
    fs.resolve_create_parent.assert_called_once_with('https://github.com/acme/docs/wiki')
    assert fs.apply_document_patch.call_args.kwargs['publish_mode'] == 'direct'


def test_replace_pending_document_creates_final_file_in_one_write():
    fs = MagicMock()
    fs.resolve_create_target.return_value = {
        'doc_id': 'acme/docs:main:articles/新文章.md',
        'uri': 'githubrepo:/acme/docs/articles/%E6%96%B0%E6%96%87%E7%AB%A0.md?ref=main',
        'browser_url': 'https://github.com/acme/docs/blob/main/articles/%E6%96%B0%E6%96%87%E7%AB%A0.md',
        'title': '新文章',
        'owner': 'acme',
        'repo': 'docs',
        'ref': 'main',
        'base_ref': 'main',
        'path': 'articles/新文章.md',
        'directory': 'articles',
        'revision': 'commit-1',
        'target_type': 'repository',
        'publish_mode': 'pull_request',
        'create_pending': False,
    }
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'target_type': 'repository',
        'doc_id': 'acme/docs:lazymind/op:articles/新文章.md',
        'uri': 'githubrepo:/acme/docs/articles/%E6%96%B0%E6%96%87%E7%AB%A0.md?ref=lazymind%2Fop',
        'revision': 'commit-2',
        'commit_sha': 'commit-2',
        'work_branch': 'lazymind/op',
        'operation_id': 'op',
        'warnings': [],
    }
    provider = GitHubWriterProvider()
    target = TargetDocument(
        uri='https://github.com/acme/docs/tree/main/articles',
        adapter='github',
        meta={
            'owner': 'acme',
            'repo': 'docs',
            'ref': 'main',
            'base_ref': 'main',
            'directory': 'articles',
            'revision': 'commit-1',
            'target_type': 'repository',
            'create_pending': True,
        },
    )

    with patch.object(provider, '_fs', return_value=fs):
        provider.replace_document('# 新文章\n\n最终正文。\n', target)

    fs.resolve_create_target.assert_called_once()
    planned_parent, planned_title = fs.resolve_create_target.call_args.args
    assert planned_parent['create_pending'] is True
    assert planned_parent['directory'] == 'articles'
    assert planned_title == '新文章'
    assert fs.apply_document_patch.call_count == 1
    assert fs.apply_document_patch.call_args.args[1] == '# 新文章\n\n最终正文。\n'
    assert target.meta['create_pending'] is False
    assert target.meta['work_branch'] == 'lazymind/op'


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


def test_replace_document_uploads_generated_image_from_writer_preview(tmp_path):
    image = tmp_path / 'generated.png'
    image_data = b'\x89PNG\r\n\x1a\nwriter-generated-image'
    image.write_bytes(image_data)
    digest = hashlib.sha256(image_data).hexdigest()
    preview_reference = (
        f'/static-files/writer-preview-assets/{digest[:2]}/{digest}.png'
        '?expires=123&sig=abc#writer-media-generated'
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
    resolved = _resolved_target()
    resolved['path'] = 'articles/market-report.md'
    target = GitHubWriterProvider._target_from_resolved(resolved)

    markdown, files = GitHubWriterProvider()._materialize_media(
        f'![市场趋势]({preview_reference})', target, library,
    )

    repository_path = f'articles/assets/{digest[:2]}/{digest}.png'
    assert markdown == f'![市场趋势](assets/{digest[:2]}/{digest}.png)'
    assert files == {repository_path: image_data}


def test_replace_document_submits_generated_image_in_same_patch(tmp_path):
    image = tmp_path / 'generated.png'
    image_data = b'\x89PNG\r\n\x1a\nwriter-generated-image'
    image.write_bytes(image_data)
    digest = hashlib.sha256(image_data).hexdigest()
    preview_reference = (
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
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'uri': _resolved_target()['uri'],
        'revision': 'commit-2',
        'warnings': [],
    }
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    with patch.object(provider, '_fs', return_value=fs):
        provider.replace_document(
            f'# 报告\n\n![市场趋势]({preview_reference})\n',
            target,
            media_assets=library,
        )

    expected_reference = f'assets/{digest[:2]}/{digest}.png'
    assert fs.apply_document_patch.call_args.args[1] == (
        f'# 报告\n\n![市场趋势]({expected_reference})\n'
    )
    assert fs.apply_document_patch.call_args.kwargs['files'] == {
        f'assets/{digest[:2]}/{digest}.png': image_data,
    }
    assert target.meta['github_writer_media_aliases'] == {
        expected_reference: 'generated-1',
    }


def test_replace_document_does_not_upload_preview_used_as_normal_link(tmp_path):
    image = tmp_path / 'generated.png'
    image_data = b'\x89PNG\r\n\x1a\nwriter-generated-image'
    image.write_bytes(image_data)
    digest = hashlib.sha256(image_data).hexdigest()
    preview_reference = (
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
    markdown = f'[查看生成图片]({preview_reference})'
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    rewritten, files = GitHubWriterProvider()._materialize_media(
        markdown, target, library,
    )

    assert rewritten == markdown
    assert files == {}


def test_replace_document_restores_imported_image_reference(tmp_path):
    image = tmp_path / 'diagram.png'
    image_data = b'preview-only'
    image.write_bytes(image_data)
    digest = hashlib.sha256(image_data).hexdigest()
    preview_reference = (
        f'/static-files/writer-preview-assets/{digest[:2]}/{digest}.png'
        '?expires=123&sig=abc#writer-media-imported'
    )
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
                    'preview_reference': preview_reference,
                    'sha256': digest,
                },
            ),
        },
    )
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    markdown, files = GitHubWriterProvider()._materialize_media(
        f'![diagram](/static-files/writer-preview-assets/{digest[:2]}/{digest}.png'
        '?expires=999&sig=changed)',
        target,
        library,
    )

    assert markdown == '![diagram](./diagram.png)'
    assert files == {}


def test_replace_document_rejects_unresolved_local_image_preview():
    fs = MagicMock()
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    with (
        patch.object(provider, '_fs', return_value=fs),
        pytest.raises(GitHubFSError) as exc_info,
    ):
        provider.replace_document(
            '# Guide\n\n![diagram](/static-files/writer-preview-assets/missing.png)\n',
            target,
        )

    assert exc_info.value.code == 'GITHUB_ASSET_INVALID'
    fs.apply_document_patch.assert_not_called()


def test_text_only_write_restores_imported_html_layout_and_repository_image_reference():
    original_layout = (
        '<table>\n'
        '  <tr>\n'
        '    <td><a href="docs/assets/artifact.jpg">'
        '<img src="docs/assets/artifact.jpg" alt="Artifact" width="100%" /></a>'
        '<br/><sub>Original caption</sub></td>\n'
        '  </tr>\n'
        '</table>'
    )
    fs = MagicMock()
    fs.resolve_target.return_value = _resolved_target()
    fs.read_bytes.side_effect = [
        f'# Original\n\n{original_layout}\n'.encode(),
        b'imported-image',
    ]
    fs.apply_document_patch.return_value = {
        'success': True,
        'provider': 'github',
        'uri': _resolved_target()['uri'],
        'revision': 'commit-2',
        'warnings': [],
    }
    provider = GitHubWriterProvider()

    with patch.object(provider, '_fs', return_value=fs):
        loaded = provider.load_document(TargetDocument(
            uri=_resolved_target()['uri'],
            adapter='github',
            meta={'target_type': 'repository'},
        ))
        preview_reference = (
            '/static-files/writer-preview-assets/aa/artifact.jpg'
            '?expires=123&sig=abc#writer-media-1'
        )
        edited = str(loaded['source_document']).replace(
            '# Original', '# Changed',
        ).replace('docs/assets/artifact.jpg', preview_reference)
        library = MediaAssetLibrary(
            library_id='library-1',
            assets={
                'asset-1': MediaAsset(
                    media_asset_id='asset-1',
                    asset_type='image',
                    source_type='input_resource',
                    meta={
                        'source_reference': 'docs/assets/artifact.jpg',
                        'preview_reference': preview_reference,
                    },
                ),
            },
        )
        provider.replace_document(
            edited,
            TargetDocument.model_validate(loaded['target_document']),
            media_assets=library,
        )

    assert fs.apply_document_patch.call_args.args[1] == (
        f'# Changed\n\n{original_layout}\n'
    )


def test_github_provider_rejects_non_markdown_repo_urls():
    assert not GitHubWriterProvider.matches(
        'https://github.com/acme/docs/blob/main/source.py'
    )
