import importlib.util
import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pytrade.protocol import command_message, validate_event
from pytrade.records import Journal
from pytrade.resources import application_root, reference_path, required_resources
from pytrade.risk import RiskBook


def load_validator():
    path = Path(__file__).resolve().parents[1] / 'scripts/verify_windows_bundle.py'
    spec = importlib.util.spec_from_file_location('bundle_validator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_pe(path, machine=0x8664):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = bytearray(128)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 0x3c, 64)
    content[64:68] = b'PE\0\0'
    struct.pack_into('<H', content, 68, machine)
    path.write_bytes(content)


class ReleaseSupportTests(unittest.TestCase):
    def test_fault_injection_rejects_normal_and_authenticated_connections(self):
        from dataclasses import replace
        from pytrade.external import ConnectionConfig
        from pytrade.market_transport_probe import drop_own_market_connection
        base = ConnectionConfig(md_front='tcp://127.0.0.1:12345', md_anonymous=True, probe_mode=True)
        for config in (replace(base, probe_mode=False), replace(base, td_front='tcp://127.0.0.1:12346'), replace(base, allow_orders=True), replace(base, user='example'), replace(base, password='example'), replace(base, auth_code='example'), replace(base, md_user='example'), replace(base, md_password='example'), replace(base, md_anonymous=False)):
            with self.assertRaises(ValueError):
                drop_own_market_connection(config)

    def test_source_resources_and_compiled_resource_root(self):
        self.assertTrue(all(path.exists() for path in required_resources()))
        with patch('sys.frozen', True, create=True), patch('sys.argv', ['/tmp/standalone/PyTrade.exe']), patch('sys.executable', '/tmp/standalone/PyTrade.exe', create=True):
            self.assertEqual(application_root(), Path('/tmp/standalone'))
            self.assertEqual(reference_path('original_dom_spec.json'), Path('/tmp/standalone/data/original_dom_spec.json'))

    def test_bundle_rejects_mixed_architecture_and_personal_state(self):
        module = load_validator()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('PyTrade.exe', 'PySide6/platforms/qwindows.dll', 'openctp_ctp/_thostmduserapi.pyd'):
                write_pe(root / name)
            for name in module.RESOURCES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            self.assertTrue(module.inspect_bundle(root)['layout_ok'])
            write_pe(root / 'legacy.dll', 0x14c)
            (root / 'connection.json').write_text('{}')
            report = module.inspect_bundle(root)
            self.assertFalse(report['layout_ok'])
            self.assertIn('legacy.dll', report['invalid_files'])
            self.assertIn('connection.json', report['invalid_files'])

    def test_journal_close_is_idempotent_and_rejects_new_work(self):
        with TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'journal.json')
            journal.submit(dict(version=2, orders={}, trades={}))
            journal.close()
            journal.close()
            with self.assertRaises(ValueError):
                journal.submit({})
            self.assertFalse(journal.thread.is_alive())

    def test_protocol_accepts_legacy_v1_and_rejects_future_or_invalid_values(self):
        self.assertEqual(command_message(dict(command='sync', cycle=1))['version'], 1)
        for message in (dict(command='sync', cycle=True), dict(command='sync', cycle=1, version=2), dict(command='execute_arbitrary'), dict(command='insert', row=[])):
            with self.assertRaises(ValueError):
                command_message(message)
        for event in (dict(kind='quote', epoch=1, key='SHFE.au2612', row={}, received=float('nan')), dict(kind='cancel_error', message='failed', order_key='one', not_sent='false'), dict(kind='td_login', front=1, session=2, day='bad')):
            with self.assertRaises(ValueError):
                validate_event(event)

    def test_close_before_open_does_not_lose_group_attribution(self):
        book = RiskBook()
        book.set_day('20261030')
        row = dict(ExchangeID='SHFE', InstrumentID='au2612', PosiDirection='2', PositionDate='1', HedgeFlag='1', Position=1)
        base = dict(TradingDay='20261030', ExchangeID='SHFE', InstrumentID='au2612', Group=2)
        orders = dict(open=base | dict(OrderSysID='open'), close=base | dict(OrderSysID='close'))
        trades = dict(close=base | dict(OrderSysID='close', Direction='1', OffsetFlag='3', Volume=2), open=base | dict(OrderSysID='open', Direction='0', OffsetFlag='0', Volume=3))
        book.reconcile([row], trades, orders)
        self.assertEqual(book.held(2), 1)
        book.reconcile([row], trades, orders)
        self.assertEqual(book.held(2), 1)


class PersistenceBoundaryTests(unittest.TestCase):
    def test_account_ledger_survives_front_change_and_migrates_legacy_history(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from pytrade.external import ConnectionConfig, ExternalSession
        from pytrade.records import transaction_paths
        config = ConnectionConfig(md_front='tcp://127.0.0.1:1', td_front='tcp://127.0.0.1:2', broker='synthetic', user='synthetic', password='synthetic')
        alternate = replace(config, td_front='tcp://127.0.0.1:3')
        commands = []
        gateway = SimpleNamespace(send=commands.append)
        with TemporaryDirectory() as directory:
            path, legacy = transaction_paths(directory, config)
            other, _ = transaction_paths(directory, alternate)
            self.assertEqual(path, other)
            legacy.parent.mkdir(parents=True)
            state = dict(version=2, orders={'unknown': dict(OrderStatus='a')}, trades={}, risk=RiskBook(dict(day='20261030', cancel_count=3, baseline='1000')).state(), observations={}, stops=[])
            legacy.write_text(json.dumps(state))
            session = ExternalSession(config, gateway, journal_path=path, legacy_journal_path=legacy)
            self.assertFalse(session.faulted)
            blocked = ExternalSession(alternate, gateway, journal_path=other)
            self.assertTrue(blocked.faulted)
            blocked.close()
            session.close()
            restored = ExternalSession(alternate, gateway, journal_path=other)
            try:
                self.assertEqual(restored.orders['unknown']['OrderStatus'], 'a')
                self.assertEqual(restored.risk.cancel_count, 3)
                self.assertEqual(restored.risk.baseline, '1000')
                self.assertTrue(legacy.exists())
                self.assertFalse(commands)
            finally:
                restored.close()

    def test_actual_disk_write_failure_never_acknowledges_durability(self):
        with TemporaryDirectory() as directory:
            writer = Journal(Path(directory) / 'journal.json')
            with patch('pytrade.records.os.replace', side_effect=OSError('disk failure')):
                writer.submit(dict(version=2, orders={}, trades={}))
                with self.assertRaises(ValueError):
                    writer.flush()
            self.assertEqual(writer.written, 0)
            with self.assertRaises(ValueError):
                writer.submit({})
            with self.assertRaises(ValueError):
                writer.close()
            self.assertFalse(writer.thread.is_alive())

    def test_account_journal_excludes_second_writer_and_unlocks_on_close(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            writer = Journal(path)
            try:
                with self.assertRaises(ValueError):
                    Journal(path)
                writer.submit(dict(version=2, orders={'first': {}}, trades={}))
            finally:
                writer.close()
            second = Journal(path)
            second.close()
            self.assertIn('first', json.loads(path.read_text())['orders'])

    def test_corrupt_top_level_journal_is_rejected(self):
        from pytrade.records import load_journal
        for value in ([], None, dict(version=True, orders={}, trades={}), dict(version=2, orders={'bad': []}, trades={})):
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'journal.json'
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_journal(path)

    def test_archive_boundary_is_not_coalesced_away(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            writer = Journal(path)
            writer.submit(dict(version=2, orders={'old': {}}, trades={}))
            writer.archive(dict(version=2, orders={'old': {}}, trades={}), '20261030')
            writer.submit(dict(version=2, orders={}, trades={}))
            writer.close()
            self.assertEqual(json.loads(path.read_text())['orders'], {})
            archives = list((path.parent / 'archive').glob('*.json'))
            self.assertEqual(len(archives), 1)
            self.assertIn('old', json.loads(archives[0].read_text())['orders'])

    def test_risk_state_is_independent_of_future_allocations(self):
        book = RiskBook()
        row = dict(ExchangeID='SHFE', InstrumentID='ag2612', PosiDirection='2', PositionDate='1', HedgeFlag='1', Position=3)
        book.assign(row, 1, 2)
        snapshot = book.state()
        book.assign(row, 1, 1)
        self.assertEqual(next(iter(snapshot['allocations'].values()))['1'], 2)
