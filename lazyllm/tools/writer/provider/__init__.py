from .base import WriterProviderBase
from .feishu import FeishuWriterProvider
from .github import GitHubWriterProvider
from .notion import NotionWriterProvider
from .registry import (
    get_writer_provider,
    match_writer_provider,
    register_writer_provider,
    resolve_writer_create_target,
)


register_writer_provider(FeishuWriterProvider)
register_writer_provider(GitHubWriterProvider)
register_writer_provider(NotionWriterProvider)


__all__ = [
    'FeishuWriterProvider',
    'GitHubWriterProvider',
    'NotionWriterProvider',
    'WriterProviderBase',
    'get_writer_provider',
    'match_writer_provider',
    'register_writer_provider',
    'resolve_writer_create_target',
]
