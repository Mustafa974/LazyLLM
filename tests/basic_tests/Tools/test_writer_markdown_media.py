import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from lazyllm.thirdparty import PIL
from lazyllm.tools.writer.data_models import MediaAssetLibrary, WritingTask
from lazyllm.tools.writer.data_models.task import InputResource
from lazyllm.tools.writer.tools.multimodal_tools import WriterMultimodalTools
from lazyllm.tools.writer.utils import extract_markdown_image_references, load_artifact_json


def _png_bytes(color=(12, 34, 56)):
    buffer = BytesIO()
    PIL.Image.new('RGB', (3, 2), color=color).save(buffer, format='PNG')
    return buffer.getvalue()


class TestMarkdownImageReferences(unittest.TestCase):

    def test_extracts_inline_and_reference_images_but_not_fenced_code(self):
        markdown = '''# 报告

## 系统设计

架构说明之前。

正文 ![系统架构图](https://img.example.com/architecture.png "架构图") 后续。

![部署图][deployment]

```markdown
![不应提取](https://img.example.com/ignored.png)
```

![本地图片](./local.png)

[deployment]: https://img.example.com/deployment.jpg "部署拓扑"
'''

        references = extract_markdown_image_references(markdown)

        self.assertEqual([item['url'] for item in references], [
            'https://img.example.com/architecture.png',
            'https://img.example.com/deployment.jpg',
            './local.png',
        ])
        self.assertEqual(references[0]['alt_text'], '系统架构图')
        self.assertEqual(references[0]['title'], '架构图')
        self.assertEqual(references[0]['heading_path'], ['报告', '系统设计'])
        self.assertEqual(references[0]['context'], '正文  后续。')
        self.assertEqual(references[0]['context_before'], '架构说明之前。')
        self.assertNotIn('ignored.png', str(references))


class TestWriterMarkdownMedia(unittest.TestCase):

    def test_remote_markdown_images_enter_library_and_merge_duplicate_content(self):
        markdown = '''# 报告

## 架构

![系统架构图](https://img.example.com/architecture.png)

![部署图][deployment]

[deployment]: https://cdn.example.com/deployment.png
'''
        image_bytes = _png_bytes()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.md'
            source.write_text(markdown, encoding='utf-8')
            tool = WriterMultimodalTools(artifact_store=str(Path(directory) / 'media'))
            with patch.object(tool, '_download_remote_image', side_effect=[
                {
                    'content': image_bytes,
                    'mime_type': 'image/png',
                    'file_name': 'architecture.png',
                    'resolved_url': 'https://img.example.com/architecture.png',
                },
                {
                    'content': image_bytes,
                    'mime_type': 'image/png',
                    'file_name': 'deployment.png',
                    'resolved_url': 'https://cdn.example.com/deployment.png',
                },
            ]) as download:
                result = tool.collect_available_media(
                    task=WritingTask(task_id='task-md', query='根据附件写报告', task_type='write'),
                    input_resources=[InputResource(
                        resource_id='input.md',
                        resource_type='file',
                        uri=str(source),
                        title='input.md',
                    )],
                )

            library = load_artifact_json(result['artifact_path'], MediaAssetLibrary)
            profiled_resources = load_artifact_json(
                result['metadata']['artifact_paths']['profile_input_resources'],
                validate_schema=False,
            )

            self.assertEqual(download.call_count, 2)
            self.assertEqual(len(library.assets), 1)
            asset = next(iter(library.assets.values()))
            self.assertEqual(asset.caption, '系统架构图')
            self.assertEqual(asset.meta['width'], 3)
            self.assertEqual(asset.meta['height'], 2)
            self.assertEqual(
                [origin['source_kind'] for origin in asset.meta['origins']],
                ['markdown_remote_image', 'markdown_remote_image'],
            )
            self.assertEqual(
                [origin['remote_url'] for origin in asset.meta['origins']],
                [
                    'https://img.example.com/architecture.png',
                    'https://cdn.example.com/deployment.png',
                ],
            )
            self.assertEqual(len(profiled_resources), 3)
            self.assertEqual(profiled_resources[0]['resource_type'], 'file')
            self.assertTrue(all(
                item['meta']['media_asset_id'] == asset.media_asset_id
                for item in profiled_resources[1:]
            ))
            self.assertEqual(Path(asset.local_path).read_bytes(), image_bytes)

    def test_download_failure_is_a_warning_and_preserves_markdown_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.md'
            source.write_text(
                '![不可访问](https://img.example.com/missing.png)',
                encoding='utf-8',
            )
            tool = WriterMultimodalTools(artifact_store=str(Path(directory) / 'media'))
            with patch.object(
                tool,
                '_download_remote_image',
                side_effect=PermissionError('forbidden'),
            ):
                result = tool.collect_available_media(
                    task=WritingTask(task_id='task-md', query='写作', task_type='write'),
                    input_resources=[InputResource(
                        resource_id='input.md',
                        resource_type='file',
                        uri=str(source),
                    )],
                )

            library = load_artifact_json(result['artifact_path'], MediaAssetLibrary)
            resources = load_artifact_json(
                result['metadata']['artifact_paths']['profile_input_resources'],
                validate_schema=False,
            )

        self.assertEqual(library.assets, {})
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]['resource_id'], 'input.md')
        self.assertIn('PermissionError: forbidden', result['metadata']['warnings'][0])

    def test_downloads_public_image_with_bounded_streaming(self):
        response = MagicMock()
        response.is_redirect = False
        response.headers = {
            'Content-Type': 'image/png; charset=binary',
            'Content-Disposition': "attachment; filename*=UTF-8''%E6%9E%B6%E6%9E%84%E5%9B%BE.png",
            'Content-Length': str(len(_png_bytes())),
        }
        response.iter_content.return_value = [_png_bytes()]
        session = MagicMock()
        session.get.return_value = response
        session_context = MagicMock()
        session_context.__enter__.return_value = session

        tool = WriterMultimodalTools(artifact_store='/tmp/writer-markdown-media-test')
        with patch.object(tool, '_internal_network_allowed', return_value=False), \
                patch(
                    'lazyllm.tools.writer.tools.multimodal_tools.socket.getaddrinfo',
                    return_value=[(2, 1, 6, '', ('93.184.216.34', 0))],
                ), patch(
                    'lazyllm.tools.writer.tools.multimodal_tools.requests.Session',
                    return_value=session_context,
                ):
            downloaded = tool._download_remote_image('https://img.example.com/a.png')

        self.assertEqual(downloaded['content'], _png_bytes())
        self.assertEqual(downloaded['mime_type'], 'image/png')
        self.assertEqual(downloaded['file_name'], '架构图.png')
        session.get.assert_called_once_with(
            'https://img.example.com/a.png',
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; LazyLLM-Writer/1.0; image-download)',
            },
            timeout=30,
            allow_redirects=False,
            stream=True,
        )
        response.close.assert_called_once()

    def test_redirect_to_private_address_is_rejected_before_second_request(self):
        response = MagicMock()
        response.is_redirect = True
        response.headers = {'Location': 'http://127.0.0.1/private.png'}
        session = MagicMock()
        session.get.return_value = response
        session_context = MagicMock()
        session_context.__enter__.return_value = session

        tool = WriterMultimodalTools(artifact_store='/tmp/writer-markdown-media-test')
        with patch.object(tool, '_internal_network_allowed', return_value=False), \
                patch(
                    'lazyllm.tools.writer.tools.multimodal_tools.socket.getaddrinfo',
                    return_value=[(2, 1, 6, '', ('93.184.216.34', 0))],
                ), patch(
                    'lazyllm.tools.writer.tools.multimodal_tools.requests.Session',
                    return_value=session_context,
                ):
            with self.assertRaisesRegex(ValueError, 'non-public address'):
                tool._download_remote_image('https://img.example.com/start.png')

        self.assertEqual(session.get.call_count, 1)
        response.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
