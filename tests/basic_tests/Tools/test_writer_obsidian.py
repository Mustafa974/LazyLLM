from unittest.mock import MagicMock

import pytest

from lazyllm.tools.fs.supplier import obsidian as obsidian_fs
from lazyllm.tools.fs.supplier.obsidian import ObsidianFS, ObsidianNote, ObsidianVault
from lazyllm.tools.writer.data_models.multimodal import MediaAsset, MediaAssetLibrary
from lazyllm.tools.writer.data_models.task import TargetDocument
from lazyllm.tools.writer.provider.obsidian import ObsidianWriterProvider


def _note(tmp_path) -> ObsidianNote:
    vault = ObsidianVault(
        vault_id='vlt_test',
        root=tmp_path,
        display_name='test',
    )
    path = tmp_path / 'note.md'
    path.write_text('', encoding='utf-8')
    return ObsidianNote(vault=vault, relative_path='note.md', path=path)


def _vault(path) -> None:
    (path / '.obsidian').mkdir(parents=True)


class TestObsidianVaultDiscovery:
    def test_discovers_nested_vaults(self, tmp_path):
        root = tmp_path / 'scan-root'
        first = root / 'Documents' / 'obs'
        second = root / 'Work' / 'knowledge'
        _vault(first)
        _vault(second)

        vaults = ObsidianFS.discover_vaults_for_root(str(root))

        assert {item.root for item in vaults} == {first, second}

    def test_discovers_a_scan_root_that_is_a_vault_without_descending_into_it(self, tmp_path):
        root = tmp_path / 'obs'
        nested = root / 'nested'
        _vault(root)
        _vault(nested)

        vaults = ObsidianFS.discover_vaults_for_root(str(root))

        assert [item.root for item in vaults] == [root]

    def test_vault_discovery_cache_expires(self, tmp_path, monkeypatch):
        root = tmp_path / 'scan-root'
        first = root / 'first'
        second = root / 'second'
        _vault(first)
        now = [100.0]
        monkeypatch.setattr(obsidian_fs.time, 'monotonic', lambda: now[0])

        first_scan = ObsidianFS.discover_vaults_for_root(str(root))
        _vault(second)
        cached_scan = ObsidianFS.discover_vaults_for_root(str(root))
        now[0] += 31.0
        refreshed_scan = ObsidianFS.discover_vaults_for_root(str(root))

        assert {item.root for item in first_scan} == {first}
        assert {item.root for item in cached_scan} == {first}
        assert {item.root for item in refreshed_scan} == {first, second}


class TestObsidianDisplayPath:
    def test_returns_the_real_path_without_a_host_mapping(self, tmp_path):
        root = tmp_path / 'scan-root'
        vault_root = root / 'obs'
        _vault(vault_root)
        note = _note(vault_root)
        fs = ObsidianFS(token=str(root))

        with obsidian_fs.config.temp('obsidian_host_root', None):
            assert fs.display_note_path(note) == str(note.path)

    def test_maps_a_container_note_path_to_the_host_scan_root(self, tmp_path):
        root = tmp_path / 'mounted-obsidian'
        vault_root = root / 'obs'
        _vault(vault_root)
        note = _note(vault_root)
        fs = ObsidianFS(token=str(root))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            assert fs.display_note_path(note) == '/Users/test/Documents/obs/note.md'


class TestObsidianHostAbsolutePath:
    def test_resolves_a_posix_host_path_inside_a_vault(self, tmp_path):
        root = tmp_path / 'mounted-obsidian'
        note_path = root / 'obs' / 'Folder' / 'Project Note #?.md'
        _vault(note_path.parent.parent)
        note_path.parent.mkdir()
        note_path.write_text('', encoding='utf-8')
        fs = ObsidianFS(token=str(root))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            note = fs.resolve_host_absolute_path(
                '/Users/test/Documents/obs/Folder/Project Note #?.md',
            )

        assert note is not None
        assert note.path == note_path
        assert note.relative_path == 'Folder/Project Note #?.md'

    def test_resolves_a_windows_host_path_inside_a_vault(self, tmp_path):
        root = tmp_path / 'mounted-obsidian'
        note_path = root / 'obs' / 'Folder' / 'Project Note.md'
        _vault(note_path.parent.parent)
        note_path.parent.mkdir()
        note_path.write_text('', encoding='utf-8')
        fs = ObsidianFS(token=str(root))

        with obsidian_fs.config.temp('obsidian_host_root', r'C:\Users\test\Documents'):
            note = fs.resolve_host_absolute_path(
                r'C:\Users\test\Documents\obs\Folder\Project Note.md',
            )

        assert note is not None
        assert note.path == note_path

    def test_returns_none_for_a_host_path_outside_a_vault(self, tmp_path):
        root = tmp_path / 'mounted-obsidian'
        vault_root = root / 'obs'
        _vault(vault_root)
        fs = ObsidianFS(token=str(root))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            assert fs.resolve_host_absolute_path('/Users/test/Desktop/note.md') is None

    def test_reports_a_missing_note_inside_a_vault(self, tmp_path):
        root = tmp_path / 'mounted-obsidian'
        vault_root = root / 'obs'
        _vault(vault_root)
        fs = ObsidianFS(token=str(root))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            with pytest.raises(FileNotFoundError, match='Obsidian note was not found'):
                fs.resolve_host_absolute_path('/Users/test/Documents/obs/missing.md')


class TestObsidianWriterProvider:
    def test_extracts_a_vault_absolute_path_as_a_canonical_uri(self, tmp_path, monkeypatch):
        root = tmp_path / 'mounted-obsidian'
        note_path = root / 'obs' / 'Folder' / 'Project (draft) [v2] #?.md'
        _vault(note_path.parent.parent)
        note_path.parent.mkdir()
        note_path.write_text('', encoding='utf-8')
        fs = ObsidianFS(token=str(root))
        vault = fs.discover_vaults()[0]
        monkeypatch.setattr(ObsidianWriterProvider, '_fs', staticmethod(lambda: fs))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            locator = ObsidianWriterProvider.find_absolute_path_locator(
                '使用写作工作流，改写 /Users/test/Documents/obs/Folder/Project (draft) [v2] #?.md',
            )

        assert locator == (
            f'obsidian://{vault.vault_id}/Folder/Project%20%28draft%29%20%5Bv2%5D%20%23%3F.md'
        )

    def test_ignores_non_vault_absolute_paths_without_initializing_fs(self, monkeypatch):
        monkeypatch.setattr(
            ObsidianWriterProvider,
            '_fs',
            staticmethod(lambda: (_ for _ in ()).throw(AssertionError('unexpected fs access'))),
        )

        assert ObsidianWriterProvider.find_absolute_path_locator('写一篇普通文章。') == ''

    def test_ignores_an_absolute_path_outside_the_vault(self, tmp_path, monkeypatch):
        root = tmp_path / 'mounted-obsidian'
        _vault(root / 'obs')
        fs = ObsidianFS(token=str(root))
        monkeypatch.setattr(ObsidianWriterProvider, '_fs', staticmethod(lambda: fs))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            locator = ObsidianWriterProvider.find_absolute_path_locator(
                '改写 /Users/test/Desktop/ordinary.md',
            )

        assert locator == ''

    def test_write_result_includes_the_host_local_path(self, tmp_path, monkeypatch):
        root = tmp_path / 'scan-root'
        vault_root = root / 'obs'
        _vault(vault_root)
        note = _note(vault_root)
        note.path.write_text('Before\n', encoding='utf-8')
        fs = ObsidianFS(token=str(root))
        provider = ObsidianWriterProvider()
        vault = fs.discover_vaults()[0]
        target = TargetDocument(
            uri=provider._canonical_uri(ObsidianNote(vault=vault, relative_path='note.md', path=note.path)),
            adapter='obsidian',
            meta={'obsidian_bridge': {'source_hash': provider._hash('Before\n')}},
        )
        monkeypatch.setattr(ObsidianWriterProvider, '_fs', staticmethod(lambda: fs))

        with obsidian_fs.config.temp('obsidian_host_root', '/Users/test/Documents'):
            result = provider.replace_document('After', target)

        assert result['local_path'] == '/Users/test/Documents/obs/note.md'
        assert target.meta['obsidian_bridge']['source_hash'] == provider._hash('After\n')
        assert note.path.read_text(encoding='utf-8') == 'After\n'

    def test_canonical_uri_escapes_and_resolves_special_path(self, tmp_path, monkeypatch):
        provider = ObsidianWriterProvider()
        vault_root = tmp_path / 'vault'
        _vault(vault_root)
        note_path = vault_root / 'Folder' / 'Project Note #?.md'
        note_path.parent.mkdir()
        note_path.write_text('', encoding='utf-8')
        vault = ObsidianVault(vault_id='vlt_test', root=vault_root, display_name='vault')
        note = ObsidianNote(
            vault=vault,
            relative_path='Folder/Project Note #?.md',
            path=note_path,
        )

        uri = provider._canonical_uri(note)
        fs = ObsidianFS(token=str(vault_root))
        monkeypatch.setattr(fs, 'discover_vaults', lambda: [vault])

        assert uri == 'obsidian://vlt_test/Folder/Project%20Note%20%23%3F.md'
        assert fs.resolve_locator(uri) == note

    def test_native_obsidian_syntax_passes_through_without_bridge_markers(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        source = (
            '> [!note]+ Original title\n'
            '> Original body\n'
            '>\n'
            '> Second paragraph\n'
            '\n'
            'See [[Original target|Visible label]].\n'
            '%% comment %%\n'
            '^block-id\n'
            '```dataview\n'
            'LIST FROM #project\n'
            '```\n'
            '![[embedded-note]]\n'
        )

        markdown, bridge = provider._to_writer_markdown(source, note, MagicMock())

        assert markdown == source
        assert 'tokens' not in bridge
        assert 'block-obsidian-' not in markdown

        restored = provider._from_writer_markdown(
            markdown.replace('Original title', 'Edited title').replace('Original body', 'Edited body'),
            bridge,
            note,
            MagicMock(),
            None,
        )

        assert restored == (
            '> [!note]+ Edited title\n'
            '> Edited body\n'
            '>\n'
            '> Second paragraph\n'
            '\n'
            'See [[Original target|Visible label]].\n'
            '%% comment %%\n'
            '^block-id\n'
            '```dataview\n'
            'LIST FROM #project\n'
            '```\n'
            '![[embedded-note]]\n'
        )

    def test_writer_output_is_not_repaired_with_hidden_tokens(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        markdown, bridge = provider._to_writer_markdown('> [!warning]- Title\n> Body\n', note, MagicMock())
        output = markdown.replace('> [!warning]- ', '')

        restored = provider._from_writer_markdown(output, bridge, note, MagicMock(), None)

        assert restored == 'Title\n> Body\n'

    def test_bridged_vault_image_round_trips_without_becoming_a_token(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        image = tmp_path / 'diagram.png'
        image.write_bytes(b'not-inspected-by-this-bridge')
        fs = MagicMock()
        fs.resolve_image_reference.return_value = image
        fs.media_uri.return_value = 'lazymind-obsidian-media://vlt_test/diagram.png?ref=img-0001'

        markdown, bridge = provider._to_writer_markdown('![[diagram.png]]\n', note, fs)
        restored = provider._from_writer_markdown(markdown, bridge, note, fs, None)

        assert markdown == '![diagram](lazymind-obsidian-media://vlt_test/diagram.png?ref=img-0001)\n'
        assert 'tokens' not in bridge
        assert restored == '![[diagram.png]]\n'

    def test_bridged_vault_image_uses_media_path_and_restores_raw_embed(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        image = tmp_path / 'diagram.png'
        image.write_bytes(b'not-inspected-by-this-bridge')
        workspace_image = tmp_path / 'writer-media.png'
        workspace_image.write_bytes(b'writer-media')
        uri = 'lazymind-obsidian-media://vlt_test/diagram.png?ref=img-0001'
        fs = MagicMock()
        fs.resolve_image_reference.return_value = image
        fs.media_uri.return_value = uri
        markdown, bridge = provider._to_writer_markdown('![[diagram.png]]\n', note, fs)
        media_assets = MediaAssetLibrary(
            library_id='media-library-test',
            assets={
                'asset-obsidian-test': MediaAsset(
                    media_asset_id='asset-obsidian-test',
                    asset_type='image',
                    source_type='input_resource',
                    uri=uri,
                    local_path=str(workspace_image),
                ),
            },
        )

        normalized = provider._normalize_materialized_image_paths(markdown, bridge, media_assets)
        restored = provider._from_writer_markdown(normalized, bridge, note, fs, media_assets)

        assert normalized == f'![diagram]({workspace_image})\n'
        assert 'lazymind-obsidian-media://' not in normalized
        assert restored == '![[diagram.png]]\n'

    def test_unmaterialized_vault_image_restores_raw_embed_before_presentation(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        image = tmp_path / 'diagram.png'
        image.write_bytes(b'not-inspected-by-this-bridge')
        fs = MagicMock()
        fs.resolve_image_reference.return_value = image
        fs.media_uri.return_value = 'lazymind-obsidian-media://vlt_test/diagram.png?ref=img-0001'
        markdown, bridge = provider._to_writer_markdown('![[diagram.png]]\n', note, fs)

        normalized = provider._normalize_materialized_image_paths(
            markdown,
            bridge,
            MediaAssetLibrary(library_id='media-library-test'),
        )

        assert normalized == '![[diagram.png]]\n'
        assert 'lazymind-obsidian-media://' not in normalized

    def test_existing_external_image_keeps_its_url_on_write_back(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        source_url = 'https://cdn.example.com/original.png'
        source = f'![Original]({source_url} "source title")\n'
        markdown, bridge = provider._to_writer_markdown(source, note, MagicMock())
        fs = MagicMock()

        restored = provider._from_writer_markdown(
            f'![Edited]({source_url} "edited title")\n',
            bridge,
            note,
            fs,
            None,
        )

        assert markdown == source
        assert bridge['external_images'] == {source_url: source_url}
        assert restored == f'![Edited]({source_url} "edited title")\n'
        fs.copy_attachment.assert_not_called()

    def test_existing_external_image_keeps_its_url_after_media_reuse(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        source_url = 'https://cdn.example.com/original.png'
        _, bridge = provider._to_writer_markdown(f'![Original]({source_url})\n', note, MagicMock())
        workspace_image = tmp_path / 'writer-media.png'
        workspace_image.write_bytes(b'writer-media')
        media_assets = MediaAssetLibrary(
            library_id='media-library-test',
            assets={
                'asset-external-test': MediaAsset(
                    media_asset_id='asset-external-test',
                    asset_type='image',
                    source_type='input_resource',
                    uri=source_url,
                    local_path=str(workspace_image),
                ),
            },
        )
        fs = MagicMock()

        restored = provider._from_writer_markdown(
            f'![Edited]({workspace_image})\n',
            bridge,
            note,
            fs,
            media_assets,
        )

        assert restored == f'![Edited]({source_url})\n'
        fs.copy_attachment.assert_not_called()

    def test_new_media_image_still_copies_into_the_vault(self, tmp_path):
        provider = ObsidianWriterProvider()
        note = _note(tmp_path)
        workspace_image = tmp_path / 'generated.png'
        workspace_image.write_bytes(b'generated')
        media_assets = MediaAssetLibrary(
            library_id='media-library-test',
            assets={
                'asset-generated-test': MediaAsset(
                    media_asset_id='asset-generated-test',
                    asset_type='image',
                    source_type='image_generation',
                    local_path=str(workspace_image),
                ),
            },
        )
        fs = MagicMock()
        fs.copy_attachment.return_value = 'assets/lazymind/generated.png'

        restored = provider._from_writer_markdown(
            f'![Generated]({workspace_image})\n',
            {},
            note,
            fs,
            media_assets,
        )

        assert restored == '![[assets/lazymind/generated.png]]\n'
        fs.copy_attachment.assert_called_once_with(note, workspace_image)
