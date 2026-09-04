import hashlib
from unittest.mock import MagicMock, patch

import requests

from lazyllm.tools.fs.client import _FSRouter
from lazyllm.tools.fs.supplier.github import GitHubFSError, GitHubRepoFS, GitHubWikiFS
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


def test_legacy_markdown_image_uri_is_restored_before_writeback(tmp_path):
    original = 'https://github.com/user-attachments/assets/original-image'
    image = tmp_path / 'image.png'
    image_data = b'\x89PNG\r\n\x1a\nlegacy-imported-image'
    image.write_bytes(image_data)
    digest = hashlib.sha256(image_data).hexdigest()
    materialized = f'assets/{digest[:2]}/{digest}.png'
    library = MediaAssetLibrary(
        library_id='library-1',
        assets={
            'asset-1': MediaAsset(
                media_asset_id='asset-1',
                asset_type='image',
                source_type='input_resource',
                uri=original,
                local_path=str(image),
                meta={'origin': 'markdown', 'sha256': digest},
            ),
        },
    )
    fs = MagicMock()
    fs.apply_document_patch.return_value = _write_result()
    provider = GitHubWriterProvider()
    target = GitHubWriterProvider._target_from_resolved(_resolved_target())

    with patch.object(provider, '_fs', return_value=fs):
        provider.replace_document(
            f'# Changed\n\n![image]({materialized})\n',
            target,
            media_assets=library,
        )

    assert fs.apply_document_patch.call_args.args[1] == (
        f'# Changed\n\n![image]({original})\n'
    )
    assert fs.apply_document_patch.call_args.kwargs['files'] == {}


def test_unsupported_code_fences_are_restored_on_writeback():
    markdown = (
        '# Original\n\n'
        '```bash\necho supported\n```\n\n'
        '```mermaid\nA --> B\n```\n\n'
        '```go\npackage main\n```\n\n'
        '```java\nclass Main {}\n```\n'
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
        target = loaded['target_document']
        provider.replace_document(
            writer_markdown.replace('# Original', '# Changed'),
            target,
        )

    assert '```bash\necho supported' in writer_markdown
    assert writer_markdown.count('```text') == 3
    assert [item['language'] for item in target.meta['github_writer_code_fences']] == [
        'mermaid', 'go', 'java',
    ]
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
        target = provider._resolve_create_target(
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
        target = provider._resolve_create_target('https://github.com/acme/docs/wiki')
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


def test_fs_router_recognizes_repo_and_wiki_browser_urls():
    router = _FSRouter()

    assert router._parse(
        'https://github.com/acme/docs/blob/main/guide.md'
    )[0] == 'githubrepo'
    assert router._parse(
        'https://github.com/acme/docs/wiki/Guide'
    )[0] == 'githubwiki'
    assert isinstance(router._get_or_create_fs('githubrepo', None), GitHubRepoFS)
    assert isinstance(router._get_or_create_fs('githubwiki', None), GitHubWikiFS)


def test_private_repository_404_is_reported_as_permission_denied():
    fs = GitHubRepoFS(token='permission-test-token', skip_instance_cache=True)
    response = MagicMock(
        status_code=404,
        headers={},
        reason='Not Found',
        content=b'{"message":"Not Found"}',
    )
    response.json.return_value = {'message': 'Not Found'}
    fs._request = MagicMock(side_effect=requests.HTTPError(response=response))

    try:
        fs._repository('private-owner', 'private-repo')
    except GitHubFSError as error:
        assert error.code == 'GITHUB_PERMISSION_DENIED'
        assert error.status_code == 403
    else:
        raise AssertionError('private repository access should fail')


def test_repo_resolve_target_keeps_revision_and_blob_sha():
    fs = GitHubRepoFS(token='resolve-test-token', skip_instance_cache=True)
    fs._repository = MagicMock(return_value={'default_branch': 'main'})
    fs._branch_head = MagicMock(return_value='commit-1')
    fs._content = MagicMock(return_value={'sha': 'blob-1', 'size': 12})

    target = fs.resolve_target('githubrepo:/acme/docs/guide.md?ref=main')

    assert target['uri'] == 'githubrepo:/acme/docs/guide.md?ref=main'
    assert target['revision'] == 'commit-1'
    assert target['blob_sha'] == 'blob-1'


def test_repo_create_flow_resolves_parent_and_new_markdown_target():
    fs = GitHubRepoFS(token='create-test-token', skip_instance_cache=True)
    fs._repository = MagicMock(return_value={
        'default_branch': 'main',
        'permissions': {'push': True},
    })
    fs._branch_head = MagicMock(return_value='commit-1')
    fs._content = MagicMock(side_effect=GitHubFSError(
        'GITHUB_TARGET_NOT_FOUND', 'Not Found', status_code=404,
    ))

    parent = fs.resolve_create_parent('https://github.com/acme/docs')
    target = fs.resolve_create_target(parent, '从零开始写文章')

    assert parent['base_ref'] == 'main'
    assert parent['create_pending'] is True
    assert target['path'] == '从零开始写文章.md'
    assert target['revision'] == 'commit-1'


def test_repo_document_patch_rejects_stale_revision():
    fs = GitHubRepoFS(token='stale-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(return_value='new-head')
    fs._commit_has_operation = MagicMock(return_value=False)

    try:
        fs.apply_document_patch(
            'githubrepo:/acme/docs/guide.md?ref=main',
            '# Updated',
            expected_revision='old-head',
            publish_mode='direct',
        )
    except GitHubFSError as error:
        assert error.code == 'GITHUB_REVISION_CONFLICT'
    else:
        raise AssertionError('stale revision should be rejected')


def test_repo_document_patch_creates_pr_and_returns_continuation_state():
    fs = GitHubRepoFS(token='pr-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(side_effect=[None, 'base-head'])
    fs._create_ref = MagicMock()
    fs._commit_files = MagicMock(return_value='commit-2')
    fs._update_ref = MagicMock()
    fs._ensure_pr = MagicMock(return_value={
        'number': 7,
        'html_url': 'https://github.com/acme/docs/pull/7',
    })

    result = fs.apply_document_patch(
        'githubrepo:/acme/docs/guide.md?ref=main',
        '# Updated',
        operation_id='operation-1',
    )

    assert result['publish_mode'] == 'pull_request'
    assert result['commit_sha'] == 'commit-2'
    assert result['work_branch'] == 'lazymind/operation-1'
    assert result['pull_request_url'].endswith('/pull/7')


def test_repo_document_patch_reuses_existing_work_branch():
    fs = GitHubRepoFS(token='continuation-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(return_value='work-head')
    fs._commit_has_operation = MagicMock(return_value=False)
    fs._commit_files = MagicMock(return_value='commit-3')
    fs._update_ref = MagicMock()
    fs._ensure_pr = MagicMock(return_value={
        'number': 7,
        'html_url': 'https://github.com/acme/docs/pull/7',
    })

    result = fs.apply_document_patch(
        'githubrepo:/acme/docs/guide.md?ref=lazymind%2Foperation-1',
        '# Updated again',
        expected_revision='work-head',
        operation_id='operation-1',
        work_branch='lazymind/operation-1',
        base_ref='main',
    )

    assert result['work_branch'] == 'lazymind/operation-1'
    fs._ensure_pr.assert_called_once_with(
        'acme', 'docs', 'lazymind/operation-1', 'main', 'Update guide.md',
    )


def test_wiki_document_patch_returns_commit(tmp_path):
    fs = GitHubWikiFS(token='wiki-test-token', skip_instance_cache=True)
    fs._clone = MagicMock(return_value=tmp_path)
    fs._git = MagicMock(side_effect=[
        'commit-2',
        'Update Guide.md via LazyMind (operation-1)',
    ])

    result = fs.apply_document_patch(
        'githubwiki:/acme/docs/Guide.md',
        '# Updated',
        expected_revision='commit-1',
        operation_id='operation-1',
    )

    assert result['commit_sha'] == 'commit-2'


def test_wiki_create_flow_resolves_root_and_new_page(tmp_path):
    fs = GitHubWikiFS(token='wiki-create-test-token', skip_instance_cache=True)
    fs._repository = MagicMock(return_value={
        'has_wiki': True,
        'permissions': {'push': True},
    })
    fs._clone = MagicMock(return_value=tmp_path)
    fs._git = MagicMock(side_effect=['commit-1', 'master', 'commit-1'])

    parent = fs.resolve_create_parent('https://github.com/acme/docs/wiki')
    target = fs.resolve_create_target(parent, '从零开始写文章')

    assert parent['target_type'] == 'wiki'
    assert parent['publish_mode'] == 'direct'
    assert parent['create_pending'] is True
    assert target['path'] == '从零开始写文章.md'
    assert target['revision'] == 'commit-1'
    assert target['uri'].startswith('githubwiki:/acme/docs/')
