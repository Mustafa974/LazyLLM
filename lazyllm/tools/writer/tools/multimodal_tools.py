from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import re
import shutil
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urljoin, urlparse

import requests
from pydantic import BaseModel, Field

from lazyllm import config
from lazyllm.components.formatter import encode_query_with_filepaths
from lazyllm.thirdparty import PIL

from .base import WriterToolBase
from ..data_models.multimodal import MediaAsset, MediaAssetLibrary, VisualPlan
from ..data_models.task import InputResource, WritingTask
from ..data_models.writer_ir import WriterDocument
from ..prompts import RESOLVE_VISUAL_NEEDS_PROMPT, VISION_SUMMARY_PROMPT
from ..utils import extract_markdown_image_references


_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_MARKDOWN_BYTES = 5 * 1024 * 1024
_MAX_REMOTE_REDIRECTS = 5
_REMOTE_IMAGE_TIMEOUT_SECONDS = 30
_REMOTE_IMAGE_USER_AGENT = 'Mozilla/5.0 (compatible; LazyLLM-Writer/1.0; image-download)'
_IMAGE_SUFFIXES = {'.bmp', '.gif', '.jpe', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp'}


class _MediaSelections(BaseModel):
    selections: Dict[str, List[str]] = Field(default_factory=dict)


class WriterMultimodalTools(WriterToolBase):
    __public_apis__ = ['collect_available_media']

    def collect_available_media(
        self,
        task: Any,
        input_resources: Any = None,
        source_document: Any = None,
    ) -> dict:
        '''Collect local and Markdown-linked images available to this writing task.'''
        writing_task = self._unified_model(task, WritingTask)
        document = self._unified_optional_model(source_document, WriterDocument)
        resources = [
            resource.model_copy(deep=True)
            for resource in [*writing_task.inputs, *self._unified_models(input_resources, InputResource)]
        ]
        library = MediaAssetLibrary(library_id=f'media-library-{writing_task.task_id or "task"}')
        warnings: List[str] = []
        markdown_media, markdown_warnings = self._extract_markdown_media(resources)
        resources.extend(markdown_media)
        warnings.extend(markdown_warnings)

        for resource in resources:
            if not self._is_image_resource(resource):
                continue
            label = resource.resource_id or resource.title or resource.uri or 'image resource'
            try:
                materialized = self._materialize_input_resource(resource)
                asset = library.assets.get(materialized.media_asset_id)
                if asset is None:
                    asset = materialized
                    library.assets[asset.media_asset_id] = asset
                else:
                    self._merge_asset_metadata(asset, materialized)
                if asset.meta.get('semantic_status') != 'ready' and self.llm is not None:
                    try:
                        asset.summary = self._describe_image(asset.local_path or '')
                        asset.meta.update(summary_source='vision_model', semantic_status='ready')
                    except Exception as exc:
                        warnings.append(f'Failed to understand {label!r}: {type(exc).__name__}: {exc}')
                resource.resource_type = 'image'
                resource.mime_type = asset.meta.get('mime_type') or resource.mime_type
                resource.summary = asset.summary
                resource.meta.update(
                    media_asset_id=asset.media_asset_id,
                    summary_source=asset.meta.get('summary_source'),
                    semantic_status=asset.meta.get('semantic_status'),
                )
            except Exception as exc:
                resource.resource_type = 'image'
                resource.meta['semantic_status'] = 'unknown'
                warnings.append(f'Failed to collect {label!r}: {type(exc).__name__}: {exc}')

        artifacts: Dict[str, Any] = {
            'media_assets': library,
            'profile_input_resources': resources,
        }
        if document is not None:
            artifacts['source_document'] = self._bind_document_media(document, library)
        return self._save_artifacts(
            artifacts,
            step_name='collect_available_media',
            primary_key='media_assets',
            context_key=None,
            summary='Collected available writing media.',
            warnings=warnings,
        ).model_dump()

    def resolve_visual_needs(self, visual_plan: Any, media_assets: Any) -> Dict[str, Any]:
        '''Reuse matching assets and request generated images for the remaining needs.'''
        plan = self._unified_model(visual_plan, VisualPlan)
        library = self._unified_model(media_assets, MediaAssetLibrary).model_copy(deep=True)
        needs = {need.need_id: need for need in plan.instructions}
        unresolved = []
        for need_id in needs:
            asset_ids = [
                asset_id for asset_id in library.visual_need_asset_ids.get(need_id, [])
                if asset_id in library.assets and self._asset_is_available(library.assets[asset_id])
            ]
            if asset_ids:
                library.visual_need_asset_ids[need_id] = asset_ids
            else:
                library.visual_need_asset_ids.pop(need_id, None)
                unresolved.append(need_id)

        available = [asset for asset in library.assets.values() if self._asset_is_available(asset)]
        warnings: List[str] = []
        if unresolved and available and self.llm is not None:
            try:
                selections = self._select_existing_assets(unresolved, needs, available)
                for need_id, asset_ids in selections.items():
                    selected = [
                        asset_id for asset_id in asset_ids
                        if asset_id in library.assets and self._asset_is_available(library.assets[asset_id])
                    ]
                    if need_id in unresolved and selected:
                        library.visual_need_asset_ids[need_id] = selected
            except Exception as exc:
                warnings.append(f'Existing media selection failed: {type(exc).__name__}: {exc}')

        acquisition_requests = []
        for need_id in unresolved:
            if library.visual_need_asset_ids.get(need_id):
                continue
            need = needs[need_id]
            if need.visual_type in {'image', 'diagram'}:
                acquisition_requests.append({
                    'instruction_id': need_id,
                    'visual_type': need.visual_type,
                    'purpose': need.purpose,
                    'strategies': ['image_generation'],
                    'required': need.required,
                })
            else:
                warnings.append(f'Visual need {need_id!r} has no MVP acquisition strategy.')

        return {
            'media_assets': library,
            'acquisition_requests': acquisition_requests,
            'warnings': warnings,
        }

    def materialize_acquired_media(
        self,
        visual_plan: Any,
        media_assets: Any,
        acquired_resources: Any,
    ) -> Dict[str, Any]:
        '''Add acquired local images to the task library and bind them to visual needs.'''
        plan = self._unified_model(visual_plan, VisualPlan)
        library = self._unified_model(media_assets, MediaAssetLibrary).model_copy(deep=True)
        resources = self._unified_raw_data(acquired_resources) or {}
        if not isinstance(resources, dict):
            raise TypeError('acquired_resources must map visual need IDs to InputResource values.')

        warnings: List[str] = []
        for need in plan.instructions:
            if library.visual_need_asset_ids.get(need.need_id):
                continue
            resource = resources.get(need.need_id)
            if resource is None:
                if need.required:
                    warnings.append(f'Required visual need {need.need_id!r} remains unresolved.')
                continue
            try:
                asset = self._materialize_input_resource(self._unified_model(resource, InputResource))
                asset = library.assets.setdefault(asset.media_asset_id, asset)
                library.visual_need_asset_ids[need.need_id] = [asset.media_asset_id]
            except Exception as exc:
                warnings.append(f'Failed to materialize {need.need_id!r}: {type(exc).__name__}: {exc}')
        return {'media_assets': library, 'warnings': warnings}

    def _select_existing_assets(
        self,
        need_ids: List[str],
        needs: Dict[str, Any],
        assets: List[MediaAsset],
    ) -> Dict[str, List[str]]:
        prompt = RESOLVE_VISUAL_NEEDS_PROMPT.format(
            visual_needs_json=json.dumps([
                {'need_id': need_id, 'visual_type': needs[need_id].visual_type, 'purpose': needs[need_id].purpose}
                for need_id in need_ids
            ], ensure_ascii=False, indent=2),
            available_media_json=json.dumps([
                {
                    'media_asset_id': asset.media_asset_id,
                    'asset_type': asset.asset_type,
                    'caption': asset.caption,
                    'summary': asset.summary,
                    'semantic_status': asset.meta.get('semantic_status'),
                }
                for asset in assets
            ], ensure_ascii=False, indent=2),
        )
        return self._call_llm_structured(prompt, _MediaSelections).selections

    def _extract_markdown_media(
        self,
        resources: List[InputResource],
    ) -> tuple[List[InputResource], List[str]]:
        extracted: List[InputResource] = []
        warnings: List[str] = []
        downloads: Dict[str, Dict[str, Any]] = {}
        for resource in resources:
            if not self._is_markdown_resource(resource):
                continue
            label = resource.resource_id or resource.title or resource.uri or 'Markdown resource'
            try:
                source = self._local_resource_path(resource)
                size = source.stat().st_size
                if not 0 < size <= _MAX_MARKDOWN_BYTES:
                    raise ValueError('Markdown file must be between 1 byte and 5 MB.')
                markdown = source.read_text(encoding='utf-8', errors='replace')
                image_references = extract_markdown_image_references(markdown)
            except Exception as exc:
                warnings.append(
                    f'Failed to inspect Markdown media in {label!r}: {type(exc).__name__}: {exc}')
                continue

            for reference in image_references:
                remote_url = str(reference.get('url') or '').strip()
                if urlparse(remote_url).scheme.lower() not in {'http', 'https'}:
                    continue
                image_label = reference.get('alt_text') or remote_url
                try:
                    downloaded = downloads.get(remote_url)
                    if downloaded is None:
                        downloaded = self._download_remote_image(remote_url)
                        downloads[remote_url] = downloaded
                    content = downloaded['content']
                    digest = hashlib.sha256(content).hexdigest()
                    suffix = self._remote_image_suffix(
                        str(downloaded.get('file_name') or ''),
                        str(downloaded.get('mime_type') or ''),
                        str(downloaded.get('resolved_url') or remote_url),
                    )
                    destination = self._remote_inputs_dir() / f'{digest}{suffix}'
                    if not destination.exists():
                        destination.write_bytes(content)
                    alt_text = str(reference.get('alt_text') or '').strip()
                    title = str(reference.get('title') or '').strip()
                    caption = alt_text or title
                    fallback_title = (
                        str(downloaded.get('file_name') or '').strip()
                        or Path(unquote(urlparse(remote_url).path)).name
                        or f'Markdown image {reference.get("source_index", 0) + 1}'
                    )
                    summary_source = 'alt_text' if alt_text else ('title' if title else 'filename')
                    origin = {
                        'provider': 'markdown',
                        'source_kind': 'markdown_remote_image',
                        'source_uri': str(source),
                        'source_resource_id': resource.resource_id or '',
                        'remote_url': remote_url,
                        'resolved_url': str(downloaded.get('resolved_url') or remote_url),
                        'alt_text': alt_text,
                        'title': title,
                        'heading_path': reference.get('heading_path') or [],
                        'context': str(reference.get('context') or ''),
                        'context_before': str(reference.get('context_before') or ''),
                        'context_after': str(reference.get('context_after') or ''),
                        'source_index': reference.get('source_index'),
                    }
                    extracted.append(InputResource(
                        resource_id=(
                            f'markdown-image:{resource.resource_id or source.name}:'
                            f'{int(reference.get("source_index") or 0) + 1}'
                        ),
                        resource_type='image',
                        uri=str(destination),
                        mime_type=str(downloaded.get('mime_type') or '') or None,
                        title=caption or fallback_title,
                        summary=caption or None,
                        meta={
                            'source_type': 'input_resource',
                            'caption': caption,
                            'summary_source': summary_source,
                            'semantic_status': 'unknown',
                            'sha256': digest,
                            'origins': [origin],
                        },
                    ))
                except Exception as exc:
                    warnings.append(
                        f'Failed to extract Markdown image {image_label!r} from {label!r}: '
                        f'{type(exc).__name__}: {exc}')
        return extracted, warnings

    def _download_remote_image(self, url: str) -> Dict[str, Any]:
        current_url = self._validate_remote_image_url(url)
        headers = {'User-Agent': _REMOTE_IMAGE_USER_AGENT}
        with requests.Session() as session:
            for _ in range(_MAX_REMOTE_REDIRECTS + 1):
                response = session.get(
                    current_url,
                    headers=headers,
                    timeout=_REMOTE_IMAGE_TIMEOUT_SECONDS,
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    if response.is_redirect:
                        location = str(response.headers.get('Location') or '').strip()
                        if not location:
                            raise ValueError('redirect response is missing Location header')
                        current_url = self._validate_remote_image_url(
                            urljoin(current_url, location))
                        continue

                    response.raise_for_status()
                    content_length = str(response.headers.get('Content-Length') or '').strip()
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError:
                            declared_size = 0
                        if declared_size > _MAX_IMAGE_BYTES:
                            raise ValueError('remote image exceeds the 20 MB download limit')
                    mime_type = str(response.headers.get('Content-Type') or '') \
                        .split(';', 1)[0].strip().lower()
                    if mime_type and not (
                        mime_type.startswith('image/') or mime_type == 'application/octet-stream'
                    ):
                        raise ValueError(f'remote URL returned non-image MIME type {mime_type!r}')
                    chunks: List[bytes] = []
                    size = 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > _MAX_IMAGE_BYTES:
                            raise ValueError('remote image exceeds the 20 MB download limit')
                        chunks.append(chunk)
                    content = b''.join(chunks)
                    if not content:
                        raise ValueError('remote image download returned no bytes')
                    return {
                        'content': content,
                        'mime_type': mime_type,
                        'file_name': self._response_file_name(response, current_url),
                        'resolved_url': current_url,
                    }
                finally:
                    response.close()
        raise ValueError('too many redirects while downloading remote image')

    @classmethod
    def _validate_remote_image_url(cls, url: str) -> str:
        normalized = str(url or '').strip()
        if not normalized or len(normalized) > 4096:
            raise ValueError('remote image URL must contain between 1 and 4096 characters')
        parsed = urlparse(normalized)
        if parsed.scheme.lower() not in {'http', 'https'}:
            raise ValueError('remote image URL scheme must be http or https')
        if not parsed.hostname:
            raise ValueError('remote image URL host is required')
        if parsed.username or parsed.password:
            raise ValueError('remote image URL credentials are not allowed')
        if cls._internal_network_allowed():
            return normalized

        hostname = parsed.hostname.rstrip('.')
        try:
            addresses = {str(ipaddress.ip_address(hostname))}
        except ValueError:
            try:
                addrinfos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise ValueError(f'could not resolve remote image host {hostname!r}') from exc
            addresses = {item[4][0] for item in addrinfos}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError('remote image host resolves to a non-public address')
        return normalized

    @staticmethod
    def _internal_network_allowed() -> bool:
        return bool(config['allow_internal_network'])

    @staticmethod
    def _response_file_name(response: requests.Response, url: str) -> str:
        disposition = str(response.headers.get('Content-Disposition') or '')
        match = re.search(
            r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)",
            disposition,
            re.IGNORECASE,
        )
        if match:
            file_name = unquote((match.group(1) or match.group(2) or '').strip())
            return file_name.replace('\\', '/').rsplit('/', 1)[-1]
        return Path(unquote(urlparse(url).path)).name

    @staticmethod
    def _remote_image_suffix(file_name: str, mime_type: str, url: str) -> str:
        for candidate in (file_name, unquote(urlparse(url).path)):
            suffix = Path(candidate).suffix.lower()
            if suffix in _IMAGE_SUFFIXES:
                return '.jpg' if suffix in {'.jpe', '.jpeg'} else suffix
        guessed = mimetypes.guess_extension(mime_type) if mime_type else None
        return '.jpg' if guessed in {'.jpe', '.jpeg'} else (guessed or '.img')

    def _remote_inputs_dir(self) -> Path:
        if not self.artifact_store:
            raise ValueError('artifact_store is not set')
        path = Path(self.artifact_store).expanduser().resolve() / 'remote-inputs'
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _local_resource_path(resource: InputResource) -> Path:
        uri = str(resource.uri or '').strip()
        parsed = urlparse(uri)
        if not uri or parsed.scheme not in {'', 'file'}:
            raise ValueError('Markdown input must use a local file path')
        path = Path(unquote(parsed.path) if parsed.scheme == 'file' else uri).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Markdown file does not exist: {path}')
        return path

    def _materialize_input_resource(self, resource: InputResource) -> MediaAsset:
        uri = str(resource.uri or '').strip()
        parsed = urlparse(uri)
        if not uri or parsed.scheme not in {'', 'file'}:
            raise ValueError('MVP image inputs must use a local file path.')
        source = Path(parsed.path if parsed.scheme == 'file' else uri).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f'image file does not exist: {source}')
        size = source.stat().st_size
        if not 0 < size <= _MAX_IMAGE_BYTES:
            raise ValueError('image file must be between 1 byte and 20 MB.')
        image_format, width, height = self._inspect_image(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        suffix = {
            'JPEG': '.jpg',
            'TIFF': '.tif',
        }.get(image_format, f'.{image_format.lower()}')
        destination = self._assets_dir() / f'{digest}{suffix}'
        if not destination.exists():
            shutil.copyfile(source, destination)

        caption = str(resource.meta.get('caption') or '').strip() or None
        summary = str(resource.summary or '').strip()
        summary_source = 'resource_summary' if summary else 'filename'
        if not summary:
            summary = (
                caption or resource.title
                or f'Image file {source.name!r}; image content has not been analyzed.'
            )
        source_type = str(resource.meta.get('source_type') or 'input_resource')
        meta = deepcopy(resource.meta)
        meta.pop('source_type', None)
        meta.pop('caption', None)
        meta.update({
            'sha256': digest,
            'mime_type': PIL.Image.MIME.get(image_format),
            'byte_size': size,
            'width': width,
            'height': height,
            'summary_source': resource.meta.get('summary_source') or summary_source,
            'semantic_status': resource.meta.get('semantic_status') or (
                'ready' if summary_source == 'resource_summary' else 'unknown'
            ),
        })
        return MediaAsset(
            media_asset_id=f'asset-{digest}',
            asset_type='generated_image' if source_type == 'image_generation' else 'image',
            source_type=source_type,
            uri=uri,
            local_path=str(destination),
            caption=caption,
            summary=summary,
            meta=meta,
        )

    @staticmethod
    def _merge_asset_metadata(existing: MediaAsset, incoming: MediaAsset) -> None:
        existing_origins = existing.meta.setdefault('origins', [])
        if not isinstance(existing_origins, list):
            existing_origins = []
            existing.meta['origins'] = existing_origins
        for origin in incoming.meta.get('origins') or []:
            if isinstance(origin, dict) and origin not in existing_origins:
                existing_origins.append(deepcopy(origin))
        if not existing.caption and incoming.caption:
            existing.caption = incoming.caption
        if not existing.summary or (
            existing.meta.get('summary_source') == 'filename' and incoming.caption
        ):
            existing.summary = incoming.summary
            existing.meta['summary_source'] = incoming.meta.get('summary_source')
            existing.meta['semantic_status'] = incoming.meta.get('semantic_status')

    @staticmethod
    def _bind_document_media(
        document: WriterDocument,
        library: MediaAssetLibrary,
    ) -> WriterDocument:
        bound = document.model_copy(deep=True)
        asset_by_block: Dict[tuple[str, str, str], str] = {}
        for asset in library.assets.values():
            for origin in asset.meta.get('origins') or []:
                if not isinstance(origin, dict):
                    continue
                key = (
                    str(origin.get('provider') or ''),
                    str(origin.get('document_id') or ''),
                    str(origin.get('block_id') or ''),
                )
                if all(key):
                    asset_by_block[key] = asset.media_asset_id
        for block in bound.iter_blocks():
            if block.type != 'image':
                continue
            key = (
                str(block.provider_binding.get('provider') or ''),
                str(block.provider_binding.get('document_id') or ''),
                str(block.provider_binding.get('block_id') or ''),
            )
            asset_id = asset_by_block.get(key)
            if not asset_id:
                continue
            block.references = [
                reference for reference in block.references
                if reference.get('type') != 'media_asset'
            ]
            block.references.append({'type': 'media_asset', 'id': asset_id})
        return bound

    def _describe_image(self, local_path: str) -> str:
        output = self.llm(
            encode_query_with_filepaths(VISION_SUMMARY_PROMPT, [local_path]),
            stream_output=False,
            llm_chat_history=[],
            lazyllm_files=None,
        )
        description = str(output).strip()
        if not description:
            raise ValueError('vision model returned an empty image description.')
        return description

    def _assets_dir(self) -> Path:
        if not self.artifact_store:
            raise ValueError('artifact_store is not set')
        path = Path(self.artifact_store).expanduser().resolve() / 'assets'
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _is_image_resource(resource: InputResource) -> bool:
        if resource.resource_type == 'image' or str(resource.mime_type or '').startswith('image/'):
            return True
        return (
            resource.resource_type == 'file'
            and Path(urlparse(resource.uri or '').path).suffix.lower() in _IMAGE_SUFFIXES
        )

    @staticmethod
    def _is_markdown_resource(resource: InputResource) -> bool:
        return (
            resource.resource_type == 'file'
            and (
                str(resource.mime_type or '').lower() == 'text/markdown'
                or Path(urlparse(resource.uri or '').path).suffix.lower() in {'.md', '.markdown'}
            )
        )

    @staticmethod
    def _asset_is_available(asset: MediaAsset) -> bool:
        return bool(asset.local_path and Path(asset.local_path).is_file())

    @staticmethod
    def _inspect_image(path: Path) -> tuple[str, int, int]:
        try:
            with PIL.Image.open(path) as image:
                image_format = str(image.format or '').upper()
                width, height = image.size
                image.verify()
        except Exception as exc:
            raise ValueError(f'file is not a valid image: {path}') from exc
        if not image_format:
            raise ValueError(f'image format cannot be detected: {path}')
        return image_format, width, height
