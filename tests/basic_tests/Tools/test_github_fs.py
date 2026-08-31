from unittest.mock import MagicMock

import pytest
import requests

from lazyllm.tools.fs.client import _FSRouter
from lazyllm.tools.fs.supplier.github import GitHubFSError, GitHubRepoFS, GitHubWikiFS


def test_fs_router_recognizes_repo_and_wiki_browser_urls():
    router = _FSRouter()

    assert router._parse(
        'https://github.com/acme/docs/blob/main/guide.md'
    ) == (
        'githubrepo', None, 'https://github.com/acme/docs/blob/main/guide.md',
    )
    assert router._parse(
        'https://github.com/acme/docs/wiki/Guide'
    ) == (
        'githubwiki', None, 'https://github.com/acme/docs/wiki/Guide',
    )
    assert isinstance(router._get_or_create_fs('githubrepo', None), GitHubRepoFS)
    assert isinstance(router._get_or_create_fs('githubwiki', None), GitHubWikiFS)


@pytest.mark.parametrize('path', ['/absolute.png', '../escape.png', 'assets//image.png'])
def test_repo_document_patch_rejects_unsafe_resource_paths(path):
    fs = GitHubRepoFS(token='unsafe-path-test-token', skip_instance_cache=True)

    with pytest.raises(ValueError, match='Invalid GitHub repository path'):
        fs.apply_document_patch(
            'githubrepo:/acme/docs/guide.md?ref=main',
            '# Updated',
            files={path: b'asset'},
        )


def test_repo_browser_url_resolves_longest_existing_branch():
    fs = GitHubRepoFS(token='browser-ref-test-token', skip_instance_cache=True)
    fs._repository = MagicMock(return_value={'default_branch': 'main'})
    fs._branch_head = MagicMock(side_effect=lambda _o, _r, ref, missing_ok=False: {
        'release/v1': 'commit-release',
    }.get(ref))

    owner, repo, ref, path = fs._parse_target(
        'https://github.com/acme/docs/blob/release/v1/guides/start.md',
        markdown_only=True,
    )

    assert (owner, repo, ref, path) == ('acme', 'docs', 'release/v1', 'guides/start.md')


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

    with pytest.raises(GitHubFSError) as captured:
        fs._repository('private-owner', 'private-repo')

    assert captured.value.code == 'GITHUB_PERMISSION_DENIED'
    assert captured.value.status_code == 403
    assert 'permission' in str(captured.value).lower()


def test_repo_resolve_target_keeps_revision_and_blob_sha():
    fs = GitHubRepoFS(token='resolve-test-token', skip_instance_cache=True)
    fs._repository = MagicMock(return_value={'default_branch': 'main'})
    fs._branch_head = MagicMock(return_value='commit-1')
    fs._content = MagicMock(return_value={'sha': 'blob-1', 'size': 12})

    target = fs.resolve_target('githubrepo:/acme/docs/guide.md?ref=main')

    assert target['uri'] == 'githubrepo:/acme/docs/guide.md?ref=main'
    assert target['revision'] == 'commit-1'
    assert target['blob_sha'] == 'blob-1'
    assert target['target_type'] == 'repository'


def test_repo_document_patch_rejects_stale_revision():
    fs = GitHubRepoFS(token='stale-direct-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(return_value='new-head')
    fs._commit_has_operation = MagicMock(return_value=False)

    with pytest.raises(GitHubFSError, match='changed') as captured:
        fs.apply_document_patch(
            'githubrepo:/acme/docs/guide.md?ref=main',
            '# Updated',
            expected_revision='old-head',
            publish_mode='direct',
        )

    assert captured.value.code == 'GITHUB_REVISION_CONFLICT'


def test_repo_pr_patch_does_not_create_branch_from_stale_base():
    fs = GitHubRepoFS(token='stale-pr-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(side_effect=[None, 'new-base-head'])
    fs._create_ref = MagicMock()

    with pytest.raises(GitHubFSError) as captured:
        fs.apply_document_patch(
            'githubrepo:/acme/docs/guide.md?ref=main',
            '# Updated',
            expected_revision='old-base-head',
            operation_id='operation-1',
        )

    assert captured.value.code == 'GITHUB_REVISION_CONFLICT'
    fs._create_ref.assert_not_called()


def test_repo_document_patch_returns_normalized_pr_result():
    fs = GitHubRepoFS(token='pr-result-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(side_effect=[None, 'base-head'])
    fs._create_ref = MagicMock()
    fs._commit_files = MagicMock(return_value='commit-2')
    fs._update_ref = MagicMock()
    fs._ensure_pr = MagicMock(return_value={'number': 7, 'html_url': 'https://github.com/acme/docs/pull/7'})

    result = fs.apply_document_patch(
        'githubrepo:/acme/docs/guide.md?ref=main',
        '# Updated',
        operation_id='operation-1',
    )

    assert result['publish_mode'] == 'pull_request'
    assert result['commit_sha'] == 'commit-2'
    assert result['work_branch'] == 'lazymind/operation-1'
    assert result['pull_request_url'].endswith('/pull/7')
    fs._create_ref.assert_called_once_with('acme', 'docs', 'lazymind/operation-1', 'base-head')


def test_repo_commit_files_does_not_create_empty_commit():
    fs = GitHubRepoFS(token='no-op-test-token', skip_instance_cache=True)
    fs._create_blob = MagicMock(return_value='existing-blob')
    fs._api = MagicMock(side_effect=[
        {'tree': {'sha': 'tree-1'}},
        {'sha': 'tree-1'},
    ])

    commit_sha = fs._commit_files(
        'acme', 'docs', 'main', 'commit-1', {'guide.md': b'# Same'}, 'No-op',
    )

    assert commit_sha == 'commit-1'
    assert fs._api.call_count == 2


def test_repo_document_patch_reuses_existing_work_branch_and_pr_base():
    fs = GitHubRepoFS(token='pr-reuse-test-token', skip_instance_cache=True)
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

    fs._ensure_pr.assert_called_once_with(
        'acme', 'docs', 'lazymind/operation-1', 'main', 'Update guide.md',
    )
    assert result['work_branch'] == 'lazymind/operation-1'
    assert result['base_ref'] == 'main'


def test_repo_document_patch_retry_reuses_commit_and_pr():
    fs = GitHubRepoFS(token='retry-test-token', skip_instance_cache=True)
    fs._branch_head = MagicMock(return_value='commit-2')
    fs._commit_has_operation = MagicMock(return_value=True)
    fs._commit_files = MagicMock()
    fs._update_ref = MagicMock()
    fs._ensure_pr = MagicMock(return_value={
        'number': 7,
        'html_url': 'https://github.com/acme/docs/pull/7',
    })

    result = fs.apply_document_patch(
        'githubrepo:/acme/docs/guide.md?ref=main',
        '# Updated',
        expected_revision='base-head',
        operation_id='operation-1',
    )

    assert result['commit_sha'] == 'commit-2'
    assert result['pull_request_number'] == 7
    fs._commit_files.assert_not_called()
    fs._update_ref.assert_not_called()


def test_wiki_uri_is_kept_separate_from_repo_scheme():
    fs = GitHubWikiFS(token='wiki-uri-test-token', skip_instance_cache=True)

    assert fs._parse_target(
        'githubwiki:/acme/docs/Getting-Started.md', document_only=True,
    ) == ('acme', 'docs', 'Getting-Started.md')
    assert fs._parse_target(
        'githubwiki:/acme/docs/_assets/image.png', document_only=False,
    ) == ('acme', 'docs', '_assets/image.png')


def test_wiki_document_patch_retry_reuses_existing_commit(tmp_path):
    fs = GitHubWikiFS(token='wiki-retry-test-token', skip_instance_cache=True)
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
    assert fs._git.call_count == 2
