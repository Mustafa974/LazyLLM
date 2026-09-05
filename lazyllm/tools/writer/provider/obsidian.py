from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, unquote, urlparse

from lazyllm.tools.fs.supplier.obsidian import (
    OBSIDIAN_IMAGE_SUFFIXES,
    ObsidianFS,
    ObsidianNote,
)

from .base import WriterProviderBase
from ..data_models.multimodal import MediaAssetLibrary
from ..data_models.task import TargetDocument
from ..data_models.writer_ir import WriterDocument, WriterStage
from ..utils import writer_document_to_markdown


_FRONTMATTER_RE = re.compile(r'\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)', re.DOTALL)
_IMAGE_EMBED_RE = re.compile(r'!\[\[([^\]\n]+)\]\]')
_WRITER_SYSTEM_ANCHOR_LINE_RE = re.compile(
    r'^[ \t]*<a\s+id=(["\'])block-[^"\']+\1(?:[ \t]+[^>]*)?[ \t]*(?:/>|>[ \t]*</a>)[ \t]*(?:\r?\n|$)',
    re.MULTILINE,
)
_MARKDOWN_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+["\'][^)]*["\'])?\)')
_LOCAL_MARKDOWN_IMAGE_RE = re.compile(
    r'!\[(?P<alt>[^\]]*)\]\((?P<target><[^>\n]+>|[^)\s]+)(?:\s+["\'][^)]*["\'])?\)'
)
_ABSOLUTE_MARKDOWN_PATH_RE = re.compile(
    r"""(?P<path>(?:[A-Za-z]:[\\/]|/)[^\r\n<>"'`]*?\.md)
        (?=$|[\s<>"'`.,;!?，。；！？、）】》」』)\]}])""",
    re.IGNORECASE | re.VERBOSE,
)


class ObsidianWriterProvider(WriterProviderBase):
    """Bridge an Obsidian Markdown note through Writer's Markdown path."""

    provider = 'obsidian'

    @classmethod
    def matches(cls, locator: str) -> bool:
        return str(locator or '').strip().lower().startswith('obsidian://')

    def resolve(self, locator: str) -> TargetDocument:
        value = str(locator or '').strip()
        if not self.matches(value):
            raise ValueError('Invalid Obsidian document locator.')
        return TargetDocument(uri=value, adapter=self.provider)

    @classmethod
    def find_absolute_path_locator(cls, user_input: str) -> str:
        """Return a canonical URI only for an absolute note path inside a Vault."""
        locator = ''
        fs: ObsidianFS | None = None
        for match in _ABSOLUTE_MARKDOWN_PATH_RE.finditer(str(user_input or '')):
            fs = fs or cls._fs()
            note = fs.resolve_host_absolute_path(match.group('path'))
            if note is None:
                continue
            candidate = cls._canonical_uri(note)
            if locator and candidate != locator:
                raise ValueError('Exactly one Obsidian document source is required.')
            locator = candidate
        return locator

    def load_document(
        self,
        target: TargetDocument,
        *,
        stage: WriterStage = 'final',
    ) -> dict:
        fs = self._fs()
        note, content = fs.read_note(str(target.uri or ''))
        markdown, bridge = self._to_writer_markdown(content, note, fs)
        resolved = target.model_copy(deep=True)
        resolved.doc_id = self._document_id(note)
        resolved.uri = self._canonical_uri(note)
        resolved.adapter = self.provider
        resolved.title = resolved.title or Path(note.relative_path).stem
        resolved.meta['obsidian_bridge'] = bridge
        return {
            'representation': 'markdown',
            'source_document': markdown,
            'target_document': resolved,
            'provider': self.provider,
            'block_count': len(markdown.splitlines()),
        }

    def create_document(self, title: str, parent_uri: str = '') -> TargetDocument:
        """Create a note in the configured default Vault.

        Obsidian has no remote parent container to resolve here: the first
        discovered Vault is the explicit local default.
        """
        note = self._fs().create_note(title)
        return TargetDocument(
            doc_id=self._document_id(note),
            uri=self._canonical_uri(note),
            adapter=self.provider,
            title=Path(note.relative_path).stem,
        )

    def replace_document(
        self,
        content: WriterDocument | str,
        target: TargetDocument,
        *,
        media_assets: MediaAssetLibrary | None = None,
    ) -> dict:
        if isinstance(content, WriterDocument):
            content = self._serialize_writer_document(content, media_assets)
        if not isinstance(content, str):
            raise TypeError('Obsidian only accepts Markdown content.')
        fs = self._fs()
        note = fs.resolve_locator(str(target.uri or ''))
        original = note.path.read_text(encoding='utf-8')
        bridge = dict(target.meta.get('obsidian_bridge') or {})
        restored = self._from_writer_markdown(content, bridge, note, fs, media_assets)
        warnings: List[str] = []
        if bridge.get('source_hash') and bridge['source_hash'] != self._hash(original):
            warnings.append('The Obsidian note changed after it was loaded; it was overwritten.')
        fs.write_note(note, restored)
        if bridge:
            bridge['source_hash'] = self._hash(restored)
            target.meta['obsidian_bridge'] = bridge
        local_path = fs.display_note_path(note)
        return {
            'doc_id': self._document_id(note),
            'adapter': self.provider,
            'locator': self._canonical_uri(note),
            'local_path': local_path,
            'block_count': len(restored.splitlines()),
            'warnings': warnings,
        }

    @staticmethod
    def _serialize_writer_document(
        document: WriterDocument,
        media_assets: MediaAssetLibrary | None,
    ) -> str:
        markdown_document = document.model_copy(deep=True)
        for block in markdown_document.iter_blocks():
            if block.type != 'image':
                continue
            reference = next(
                (
                    item for item in block.references
                    if item.get('type') == 'media_asset' and item.get('id')
                ),
                None,
            )
            asset = (
                media_assets.assets.get(str(reference['id']))
                if reference is not None and media_assets is not None
                else None
            )
            if asset is not None and asset.local_path:
                reference['path'] = asset.local_path
        return writer_document_to_markdown(markdown_document)

    @staticmethod
    def _fs() -> ObsidianFS:
        return ObsidianFS()

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    @staticmethod
    def _document_id(note: ObsidianNote) -> str:
        return f'{note.vault.vault_id}:{note.relative_path}'

    @staticmethod
    def _canonical_uri(note: ObsidianNote) -> str:
        return f'obsidian://{note.vault.vault_id}/{quote(note.relative_path, safe="/")}'

    def _to_writer_markdown(
        self,
        content: str,
        note: ObsidianNote,
        fs: ObsidianFS,
    ) -> tuple[str, Dict[str, Any]]:
        frontmatter = ''
        matched = _FRONTMATTER_RE.match(content)
        if matched:
            frontmatter = matched.group(0)
            content = content[matched.end():]
        bridge: Dict[str, Any] = {
            'source_hash': self._hash(frontmatter + content),
            'frontmatter': frontmatter,
            'images': {},
            'external_images': {},
            'warnings': [],
        }
        images: Dict[str, Dict[str, Any]] = bridge['images']
        external_images: Dict[str, str] = bridge['external_images']
        warnings: List[str] = bridge['warnings']

        def unsupported_image(raw: str, reason: str) -> str:
            warnings.append(reason)
            return raw

        def bridge_image(
            raw: str,
            reference: str,
            alt: str,
            *,
            markdown_relative: bool,
        ) -> str:
            try:
                source = fs.resolve_image_reference(
                    note,
                    reference,
                    markdown_relative=markdown_relative,
                )
            except (FileNotFoundError, ValueError) as exc:
                return unsupported_image(raw, f'Obsidian image was kept without import: {exc}')
            reference_id = f'img-{len(images) + 1:04x}'
            uri = fs.media_uri(note, source, reference_id)
            images[uri] = {'raw': raw}
            return f'![{alt or source.stem}]({uri})'

        def obsidian_image(match: re.Match[str]) -> str:
            raw = match.group(0)
            reference = match.group(1)
            target = reference.split('|', 1)[0].split('#', 1)[0].strip()
            suffix = Path(target).suffix.lower()
            if suffix in OBSIDIAN_IMAGE_SUFFIXES:
                alias = reference.partition('|')[2].strip()
                alt = alias if alias and not alias.isdigit() else ''
                return bridge_image(raw, reference, alt, markdown_relative=False)
            return raw

        def local_markdown_image(match: re.Match[str]) -> str:
            raw = match.group(0)
            raw_target = match.group('target').strip()
            target = raw_target
            if target.startswith('<') and target.endswith('>'):
                target = target[1:-1].strip()
            parsed = urlparse(target)
            if parsed.scheme.lower() in {'http', 'https'} or target.startswith('//'):
                media_uri = f'https:{target}' if target.startswith('//') else target
                external_images[media_uri] = raw_target
                return raw
            if parsed.scheme:
                return raw
            suffix = Path(unquote(parsed.path)).suffix.lower()
            if suffix in OBSIDIAN_IMAGE_SUFFIXES:
                return bridge_image(
                    raw,
                    target,
                    match.group('alt').strip(),
                    markdown_relative=True,
                )
            return raw

        content = _IMAGE_EMBED_RE.sub(obsidian_image, content)
        content = _LOCAL_MARKDOWN_IMAGE_RE.sub(local_markdown_image, content)
        return content, bridge

    def _from_writer_markdown(
        self,
        content: str,
        bridge: Dict[str, Any],
        note: ObsidianNote,
        fs: ObsidianFS,
        media_assets: MediaAssetLibrary | None,
    ) -> str:
        content = self._restore_images(content, bridge, note, fs, media_assets)
        content = _WRITER_SYSTEM_ANCHOR_LINE_RE.sub('', content)
        frontmatter = str(bridge.get('frontmatter') or '')
        return frontmatter + content.strip() + '\n'

    @staticmethod
    def _normalize_materialized_image_paths(
        content: str,
        bridge: Dict[str, Any],
        media_assets: MediaAssetLibrary,
    ) -> str:
        """Replace bridge-only image URIs with existing Writer media paths."""
        images = {
            str(uri): dict(item)
            for uri, item in dict(bridge.get('images') or {}).items()
            if isinstance(item, dict)
        }
        paths = {
            str(asset.uri or ''): str(asset.local_path or '')
            for asset in media_assets.assets.values()
            if str(asset.uri or '').strip() and str(asset.local_path or '').strip()
        }
        for uri, item in images.items():
            local_path = paths.get(uri)
            if local_path:
                content = content.replace(uri, local_path)
                continue
            raw = str(item.get('raw') or '')
            if raw:
                content = re.sub(
                    r'!\[[^\]]*\]\(' + re.escape(uri) + r'\)',
                    lambda _match, raw=raw: raw,
                    content,
                )
        return content

    def _restore_images(
        self,
        content: str,
        bridge: Dict[str, Any],
        note: ObsidianNote,
        fs: ObsidianFS,
        media_assets: MediaAssetLibrary | None,
    ) -> str:
        assets = list((media_assets.assets if media_assets else {}).values())
        images = {
            str(uri): dict(item)
            for uri, item in dict(bridge.get('images') or {}).items()
            if isinstance(item, dict)
        }
        external_images = {
            str(uri): str(raw_uri)
            for uri, raw_uri in dict(bridge.get('external_images') or {}).items()
            if str(uri).strip() and str(raw_uri).strip()
        }

        def replacement(match: re.Match[str]) -> str:
            uri = match.group(2)
            reference = uri[1:-1].strip() if uri.startswith('<') and uri.endswith('>') else uri
            media_uri = f'https:{reference}' if reference.startswith('//') else reference
            original_external = external_images.get(media_uri)
            if original_external is None:
                for asset in assets:
                    if reference != str(asset.local_path or ''):
                        continue
                    original_external = external_images.get(str(asset.uri or ''))
                    if original_external is not None:
                        break
            if original_external is not None:
                return match.group(0).replace(uri, original_external, 1)
            original = self._bridged_image_raw(uri, images, assets)
            if original is not None:
                return original
            source = self._asset_path(uri, assets)
            if source is None:
                return match.group(0)
            return f'![[{fs.copy_attachment(note, source)}]]'

        return _MARKDOWN_IMAGE_RE.sub(replacement, content)

    @staticmethod
    def _bridged_image_raw(
        uri: str,
        images: Dict[str, Dict[str, Any]],
        assets: list[Any],
    ) -> str | None:
        for candidate in (uri, unquote(uri)):
            item = images.get(candidate)
            raw = str((item or {}).get('raw') or '')
            if raw:
                return raw
        for asset in assets:
            source_uri = str(asset.uri or '')
            item = images.get(source_uri)
            if not item:
                continue
            local_path = str(asset.local_path or '')
            if uri != local_path:
                continue
            raw = str(item.get('raw') or '')
            if raw:
                return raw
        return None

    @staticmethod
    def _asset_path(uri: str, assets: list[Any]) -> Path | None:
        parsed = urlparse(uri)
        candidate = Path(unquote(parsed.path) if parsed.scheme == 'file' else uri)
        if candidate.is_file():
            return candidate
        for asset in assets:
            values = {str(asset.uri or ''), str(asset.local_path or '')}
            if uri not in values:
                continue
            local = Path(str(asset.local_path or ''))
            if local.is_file():
                return local
        return None


__all__ = ['ObsidianWriterProvider']
