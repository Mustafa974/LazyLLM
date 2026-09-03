from unittest.mock import MagicMock

import requests

from lazyllm.tools.fs.client import _FSRouter
from lazyllm.tools.fs.supplier.github import GitHubFSError, GitHubRepoFS, GitHubWikiFS


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
