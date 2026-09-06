from .base import WriterProviderBase
from .feishu import FeishuWriterProvider
from .notion import NotionWriterProvider
from .obsidian import ObsidianWriterProvider
from .registry import (
    get_writer_provider,
    match_writer_provider,
    register_writer_provider,
)


register_writer_provider(FeishuWriterProvider)
register_writer_provider(NotionWriterProvider)
register_writer_provider(ObsidianWriterProvider)


__all__ = [
    'FeishuWriterProvider',
    'NotionWriterProvider',
    'ObsidianWriterProvider',
    'WriterProviderBase',
    'get_writer_provider',
    'match_writer_provider',
    'register_writer_provider',
]
