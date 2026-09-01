from __future__ import annotations

import hashlib
import mimetypes
import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

from ...fs.supplier.github import GitHubFSError, GitHubRepoFS, GitHubWikiFS
from ..data_models.multimodal import MediaAssetLibrary
from ..data_models.task import InputResource, TargetDocument
from ..data_models.writer_ir import WriterDocument, WriterStage
from .base import WriterProviderBase

_GITHUB_REPO_URL_RE = re.compile(
    r'^https?://(?:www\.)?github\.com/[^/]+/[^/]+/blob/.+\.(?:md|markdown)(?:[?#].*)?$',
    re.IGNORECASE,
)
_GITHUB_WIKI_URL_RE = re.compile(
    r'^https?://(?:www\.)?github\.com/[^/]+/[^/]+/wiki/[^?#]+',
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(
    r'(?P<image>!)?\[[^\]]*\]\(\s*(?P<url><[^>]+>|[^\s)]+)(?:\s+["\'][^)]*["\'])?\s*\)',
)
_HTML_MEDIA_RE = re.compile(
    r'<(?:img|source|video|audio)\b[^>]*?\bsrc=["\'](?P<url>[^"\']+)["\']',
    re.IGNORECASE,
)
_MARKDOWN_SUFFIXES = {'.md', '.markdown'}
_MAX_ASSET_BYTES = 20 * 1024 * 1024
_MAX_WRITE_ASSET_BYTES = 50 * 1024 * 1024


def _target_type_from_locator(locator: str) -> str:
    value = str(locator or '').strip().lower()
    if value.startswith('githubwiki:') or _GITHUB_WIKI_URL_RE.match(value):
        return 'wiki'
    if value.startswith('githubrepo:') or _GITHUB_REPO_URL_RE.match(value):
        return 'repository'
    return ''


def _image_suffix(data: bytes) -> str:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if data.startswith(b'BM'):
        return '.bmp'
    if data.startswith((b'II*\x00', b'MM\x00*')):
        return '.tiff'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    prefix = data.lstrip(b'\xef\xbb\xbf\x00\x09\x0a\x0d\x20')[:4096].lower()
    if b'<svg' in prefix and b'<!doctype' not in prefix and b'<!entity' not in prefix:
        return '.svg'
    raise GitHubFSError(
        'GITHUB_ASSET_INVALID',
        'GitHub Writer assets must be supported image files.',
    )


class GitHubWriterProvider(WriterProviderBase):
    """Keep GitHub repository and Wiki Writer documents as native Markdown."""

    provider = 'github'

    @classmethod
    def matches(cls, locator: str) -> bool:
        value = str(locator or '').strip()
        return bool(
            value.lower().startswith(('githubrepo:/', 'githubwiki:/'))
            or _GITHUB_REPO_URL_RE.match(value)
            or _GITHUB_WIKI_URL_RE.match(value)
        )

    @staticmethod
    def _fs(target_type: str):
        if target_type == 'repository':
            return GitHubRepoFS(dynamic_auth=True)
        if target_type == 'wiki':
            return GitHubWikiFS(dynamic_auth=True)
        raise ValueError(f'Unsupported GitHub target type: {target_type!r}.')

    def resolve(self, locator: str) -> TargetDocument:
        value = str(locator or '').strip()
        target_type = _target_type_from_locator(value)
        if not target_type:
            raise ValueError(f'Invalid GitHub Writer document locator: {locator!r}.')
        resolved = self._fs(target_type).resolve_target(value)
        return self._target_from_resolved(resolved)

    @staticmethod
    def _target_from_resolved(resolved: Mapping[str, object]) -> TargetDocument:
        meta = {
            key: value
            for key, value in resolved.items()
            if key not in {'doc_id', 'uri', 'title'} and value is not None
        }
        return TargetDocument(
            doc_id=str(resolved.get('doc_id') or '') or None,
            uri=str(resolved.get('uri') or '') or None,
            adapter='github',
            title=str(resolved.get('title') or '') or None,
            meta=meta,
        )

    @staticmethod
    def _locator(target: TargetDocument) -> str:
        locator = str(target.uri or target.meta.get('browser_url') or '').strip()
        if not locator:
            raise ValueError('GitHub target_document must provide uri or browser_url.')
        return locator

    @staticmethod
    def _target_type(target: TargetDocument) -> str:
        target_type = str(target.meta.get('target_type') or '').strip().lower()
        return target_type or _target_type_from_locator(str(target.uri or ''))

    def load_document(
        self,
        target: TargetDocument,
        *,
        stage: WriterStage = 'final',
    ) -> dict:
        del stage  # Markdown representation does not carry Writer IR stages.
        target_type = self._target_type(target)
        fs = self._fs(target_type)
        resolved = fs.resolve_target(self._locator(target))
        resolved_target = self._merge_target(target, resolved)
        try:
            markdown = fs.read_bytes(str(resolved['uri'])).decode('utf-8')
        except UnicodeDecodeError as exc:
            raise GitHubFSError(
                'GITHUB_FORMAT_UNSUPPORTED',
                'GitHub Writer documents must be UTF-8 Markdown.',
            ) from exc
        resources, warnings = self._collect_referenced_resources(
            markdown, resolved_target, fs,
        )
        return {
            'representation': 'markdown',
            'source_document': markdown,
            'target_document': resolved_target,
            'provider': self.provider,
            'block_count': 1,
            'input_resources': resources,
            'resource_warnings': warnings,
        }

    def _collect_referenced_resources(
        self,
        markdown: str,
        target: TargetDocument,
        fs: GitHubRepoFS | GitHubWikiFS,
    ) -> tuple[list[InputResource], list[str]]:
        candidates: list[tuple[str, bool]] = []
        candidates.extend(
            (match.group('url').strip('<>'), bool(match.group('image')))
            for match in _MARKDOWN_LINK_RE.finditer(markdown)
        )
        candidates.extend(
            (match.group('url').strip(), True)
            for match in _HTML_MEDIA_RE.finditer(markdown)
        )
        resources: list[InputResource] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for raw_url, image_hint in candidates:
            uri = self._referenced_uri(raw_url, target)
            if not uri or uri in seen:
                continue
            seen.add(uri)
            suffix = PurePosixPath(urlparse(uri).path).suffix.lower()
            if suffix in _MARKDOWN_SUFFIXES:
                continue
            mime_type = mimetypes.guess_type(unquote(urlparse(uri).path))[0]
            resource_type = 'image' if image_hint or str(mime_type or '').startswith('image/') else 'file'
            try:
                payload = fs.read_bytes(uri)
            except Exception as exc:  # noqa: BLE001 - one resource failure becomes a warning.
                code = getattr(exc, 'code', type(exc).__name__)
                warnings.append(f'{raw_url}: {code}')
                continue
            resources.append(InputResource(
                resource_id=f'github-resource-{len(resources)}',
                resource_type=resource_type,
                uri=uri,
                mime_type=mime_type,
                title=PurePosixPath(unquote(urlparse(uri).path)).name or None,
                summary=None,
                meta={
                    'provider': self.provider,
                    'role': 'background',
                    'referenced_from': target.uri,
                    'source_reference': raw_url,
                    'size': len(payload),
                },
            ))
        return resources, warnings

    def _referenced_uri(self, raw_url: str, target: TargetDocument) -> str:
        value = unquote(str(raw_url or '').strip())
        if not value or value.startswith(('#', 'data:', 'mailto:', 'javascript:')):
            return ''
        target_type = self._target_type(target)
        owner = str(target.meta.get('owner') or '')
        repo = str(target.meta.get('repo') or '')
        document_path = str(target.meta.get('path') or '')
        if not owner or not repo or not document_path:
            return ''
        parsed = urlparse(value)
        if parsed.scheme in ('http', 'https'):
            if (parsed.hostname or '').lower() not in {'github.com', 'www.github.com'}:
                return ''
            if target_type != 'repository':
                return ''
            parts = [part for part in parsed.path.split('/') if part]
            if len(parts) < 5 or parts[0] != owner or parts[1] != repo or parts[2] != 'blob':
                return ''
            # Referenced browser URLs are normally generated with the same ref
            # as the source document, so remove that exact prefix without
            # guessing another branch boundary.
            ref = str(target.meta.get('ref') or '')
            tail = unquote('/'.join(parts[3:]))
            if not ref or not tail.startswith(ref + '/'):
                return ''
            resource_path = tail[len(ref) + 1:]
        else:
            relative_path = unquote(parsed.path)
            if relative_path.startswith('/'):
                resource_path = relative_path.lstrip('/')
            else:
                resource_path = posixpath.normpath(
                    posixpath.join(posixpath.dirname(document_path), relative_path),
                )
            if resource_path == '..' or resource_path.startswith('../'):
                return ''
        if target_type == 'wiki':
            return f'githubwiki:/{owner}/{repo}/{quote(resource_path, safe="/")}'
        ref = str(target.meta.get('ref') or '')
        return (
            f'githubrepo:/{owner}/{repo}/{quote(resource_path, safe="/")}'
            f'?ref={quote(ref, safe="")}'
        )

    def create_document(self, title: str, parent_uri: str = '') -> TargetDocument:
        title = str(title or '').strip()
        if not title:
            raise ValueError('title is required')
        parent_uri = str(parent_uri or '').strip()
        target_type = _target_type_from_locator(parent_uri)
        if not target_type:
            raise ValueError('GitHub create_document requires a repository or Wiki parent URI.')
        created = self._fs(target_type).create_document(
            title,
            parent_uri,
            operation_id=hashlib.sha256(
                f'create:{parent_uri}:{title}'.encode(),
            ).hexdigest()[:20],
        )
        return self._target_from_resolved(created)

    def replace_document(
        self,
        content: WriterDocument | str,
        target: TargetDocument,
        *,
        media_assets: MediaAssetLibrary | None = None,
    ) -> dict:
        if isinstance(content, WriterDocument):
            raise TypeError('GitHub Writer Provider accepts final Markdown, not WriterDocument IR.')
        if not isinstance(content, str):
            raise TypeError('GitHub Writer Provider content must be a Markdown string.')
        target_type = self._target_type(target)
        fs = self._fs(target_type)
        if not target.meta.get('revision'):
            refreshed = fs.resolve_target(self._locator(target))
            target = self._merge_target(target, refreshed)
        markdown, files = self._materialize_media(content, target, media_assets)
        publish_mode = str(target.meta.get('publish_mode') or target.meta.get('write_mode') or '').strip()
        if not publish_mode:
            publish_mode = 'direct' if target_type == 'wiki' else 'pull_request'
        requested_operation_id = str(target.meta.get('operation_id') or '').strip()
        last_operation_id = str(target.meta.get('last_operation_id') or '').strip()
        result = fs.apply_document_patch(
            self._locator(target),
            markdown,
            files=files,
            expected_revision=str(target.meta.get('revision') or ''),
            operation_id=(
                requested_operation_id
                if requested_operation_id != last_operation_id else ''
            ),
            publish_mode=publish_mode,
            commit_message=str(target.meta.get('commit_message') or ''),
            pull_request_title=str(target.meta.get('pull_request_title') or ''),
            work_branch=str(target.meta.get('work_branch') or ''),
            base_ref=str(target.meta.get('base_ref') or ''),
        )
        self._apply_write_result(target, result)
        return {
            **result,
            'doc_id': str(result.get('doc_id') or target.doc_id or ''),
            'adapter': self.provider,
            'locator': str(result.get('uri') or target.uri or ''),
            'block_count': 1,
            'warnings': list(result.get('warnings') or []),
        }

    def append_document(
        self,
        content: WriterDocument | str,
        target: TargetDocument,
        *,
        media_assets: MediaAssetLibrary | None = None,
    ) -> dict:
        if isinstance(content, WriterDocument):
            raise TypeError('GitHub Writer Provider accepts final Markdown, not WriterDocument IR.')
        loaded = self.load_document(target)
        current = str(loaded['source_document'])
        resolved_target = TargetDocument.model_validate(loaded['target_document'])
        separator = '' if not current or current.endswith('\n') else '\n'
        return self.replace_document(
            f'{current}{separator}{content}', resolved_target, media_assets=media_assets,
        )

    @staticmethod
    def _merge_target(target: TargetDocument, resolved: Mapping[str, object]) -> TargetDocument:
        merged = target.model_copy(deep=True)
        refreshed = GitHubWriterProvider._target_from_resolved(resolved)
        merged.doc_id = refreshed.doc_id
        merged.uri = refreshed.uri
        merged.title = refreshed.title or merged.title
        merged.adapter = 'github'
        merged.meta = {**merged.meta, **refreshed.meta}
        return merged

    @staticmethod
    def _apply_write_result(target: TargetDocument, result: Mapping[str, object]) -> None:
        target.doc_id = str(result.get('doc_id') or target.doc_id or '') or None
        target.uri = str(result.get('uri') or target.uri or '') or None
        target.adapter = 'github'
        target.meta = {
            **target.meta,
            **{
                key: value
                for key, value in result.items()
                if key not in {'success', 'provider', 'doc_id', 'uri', 'warnings'}
            },
        }
        operation_id = str(result.get('operation_id') or '').strip()
        if operation_id:
            target.meta['last_operation_id'] = operation_id

    def _materialize_media(
        self,
        markdown: str,
        target: TargetDocument,
        media_assets: MediaAssetLibrary | None,
    ) -> tuple[str, dict[str, bytes]]:
        if media_assets is None:
            return markdown, {}
        document_path = str(target.meta.get('path') or '')
        document_dir = posixpath.dirname(document_path)
        target_type = self._target_type(target)
        asset_dir = '_assets' if target_type == 'wiki' else 'assets'
        files: dict[str, bytes] = {}
        rewritten = self._restore_imported_media_references(markdown, media_assets)
        used_paths: set[str] = set()
        total_size = 0
        for asset_id, asset in media_assets.assets.items():
            marker = f'asset://{asset_id}'
            if marker not in rewritten:
                continue
            local_path = str(asset.local_path or '').strip()
            if not local_path or not os.path.isfile(local_path):
                raise GitHubFSError(
                    'GITHUB_ASSET_INVALID',
                    f'Media asset {asset_id!r} has no readable local_path.',
                )
            data = Path(local_path).read_bytes()
            if not data or len(data) > _MAX_ASSET_BYTES:
                raise GitHubFSError(
                    'GITHUB_ASSET_TOO_LARGE',
                    f'Media asset {asset_id!r} must be between 1 byte and 20 MB.',
                )
            total_size += len(data)
            if total_size > _MAX_WRITE_ASSET_BYTES:
                raise GitHubFSError(
                    'GITHUB_ASSET_TOO_LARGE',
                    'GitHub Writer assets exceed the 50 MB per-write limit.',
                )
            digest = hashlib.sha256(data).hexdigest()
            suffix = _image_suffix(data)
            filename = f'{digest[:2]}/{digest}{suffix}'
            relative_target = posixpath.join(document_dir, asset_dir, filename)
            if relative_target in used_paths:
                rewritten = rewritten.replace(
                    marker,
                    quote(posixpath.relpath(relative_target, document_dir or '.'), safe='/._-'),
                )
                continue
            used_paths.add(relative_target)
            files[relative_target] = data
            link = posixpath.relpath(relative_target, document_dir or '.')
            rewritten = rewritten.replace(marker, quote(link, safe='/._-'))
        return rewritten, files

    @staticmethod
    def _restore_imported_media_references(
        markdown: str,
        media_assets: MediaAssetLibrary,
    ) -> str:
        replacements: dict[str, str] = {}
        for asset in media_assets.assets.values():
            source_reference = str(asset.meta.get('source_reference') or '').strip()
            if not source_reference:
                continue
            for candidate in (
                str(asset.local_path or '').strip(),
                str(asset.meta.get('preview_reference') or '').strip(),
            ):
                if candidate:
                    replacements[candidate] = source_reference
        if not replacements:
            return markdown

        def replace_url(match: re.Match, *, image_only: bool) -> str:
            if image_only and not match.groupdict().get('image'):
                return match.group(0)
            token = str(match.group('url') or '')
            value = token.strip('<>')
            replacement = replacements.get(value)
            if not replacement:
                return match.group(0)
            if token.startswith('<') and token.endswith('>'):
                replacement = f'<{replacement}>'
            start, end = match.span('url')
            offset = match.start()
            return (
                match.group(0)[:start - offset]
                + replacement
                + match.group(0)[end - offset:]
            )

        restored = _MARKDOWN_LINK_RE.sub(
            lambda match: replace_url(match, image_only=True),
            markdown,
        )
        return _HTML_MEDIA_RE.sub(
            lambda match: replace_url(match, image_only=False),
            restored,
        )


__all__ = ['GitHubWriterProvider']
