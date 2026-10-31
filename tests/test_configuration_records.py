import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pytrade.configuration import ConfigStore, parse_legacy
from pytrade.records import Journal, export_csv, load_journal


class ConfigurationRecordsTests(unittest.TestCase):
    def test_legacy_gb_duplicates_unknown_zero_and_secrets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.xml'
            path.write_bytes('[显示]\nMainFont=新宋体\nTickRowHeights=12\nPriceListMargin=5\nPriceListMargin=0\nUnknown=0\nPW=private\nMOrderTime1=10.29.58\nHQFFPORT=999999\nPriceListRatios=10,-1\n'.encode('gb18030'))
            parsed = parse_legacy(path)
            self.assertEqual(parsed['candidates']['row_height'], 12)
            margins = [item['value'] for item in parsed['entries'] if item['key'] == 'PriceListMargin']
            self.assertEqual(margins, ['5', '0'])
            self.assertNotIn('private', json.dumps(parsed))
            self.assertGreaterEqual(len(parsed['diagnostics']), 5)
            store = ConfigStore(directory)
            store.set_overrides(dict(font='local', row_height=16, dom_height=800, wheel_steps=2))
            store.apply_import(parsed)
            restored = ConfigStore(directory)
            self.assertEqual(restored.effective().row_height, 16)
            self.assertEqual(restored.legacy, parsed['entries'])

    def test_invalid_config_preserved_with_default_display(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            path.write_text('{broken')
            store = ConfigStore(directory)
            self.assertEqual(store.effective().row_height, 13)
            with self.assertRaises(ValueError):
                store.set_overrides(dict(font='test', row_height=14, dom_height=900, wheel_steps=3))
            self.assertEqual(path.read_text(encoding="utf-8"), '{broken')

    def test_journal_ordering_and_migration_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'trades.json'
            original = dict(version=1, orders={}, trades={})
            path.write_text(json.dumps(original))
            load_journal(path)
            self.assertTrue(path.with_suffix('.v1.backup.json').exists())
            journal = Journal(path)
            try:
                for number in range(20):
                    journal.submit(dict(version=2, orders={'last': number}, trades={}))
                journal.flush()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))['orders']['last'], 19)
                self.assertEqual(journal.written, 20)
            finally:
                journal.close()

    def test_writer_failure_is_reported(self):
        with TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'trades.json')
            with patch('pytrade.records.os.replace', side_effect=OSError('disk full')):
                journal.submit(dict(version=2, orders={}, trades={}))
                with self.assertRaises(ValueError):
                    journal.flush()
            with self.assertRaises(ValueError):
                journal.submit({})
            with self.assertRaises(ValueError):
                journal.close()

    def test_csv_neutralizes_formulas(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'export.csv'
            export_csv(path, ['value'], [['=SUM(A1:A2)'], ['  @cmd'], ['normal']])
            content = path.read_text(encoding='utf-8-sig')
            self.assertIn("'=SUM", content)
            self.assertIn("'  @cmd", content)


class ReplayTests(unittest.TestCase):
    def test_recovery_fixture_is_readonly_and_preserves_terminal_state(self):
        from pytrade.replay import replay_session
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/session_recovery.json'
        result = replay_session(fixture)
        self.assertEqual(result['order_states']['20261030:1:2:10'], '0')
        self.assertEqual(result['trades'], 1)
        self.assertFalse(result['logged_in'])

    def test_replay_rejects_credential_fields(self):
        from pytrade.replay import replay_session
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / 'bad.json'
            for field in ('password', 'md_user', 'md_password'):
                with self.subTest(field=field):
                    fixture.write_text(json.dumps(dict(version=1, source='synthetic', events=[]) | {field: 'do-not-log'}))
                    with self.assertRaises(ValueError):
                        replay_session(fixture)
