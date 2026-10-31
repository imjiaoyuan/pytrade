import errno
import queue
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from pytrade.sounds import SoundTracker, SOUND_FILES
from pytrade.transport import check_market_transport


class SoundTransportTests(unittest.TestCase):
    def test_original_audio_copies_match_without_modifying_originals(self):
        root = Path(__file__).resolve().parents[1]
        sources = [root / 'reference_materials' / 'original_software', root / 'reference_materials' / 'archive_review_20261030' / 'extracted', root / 'assets' / 'sounds']
        for filename in SOUND_FILES.values():
            source = next(directory for directory in sources if (directory / filename).exists())
            self.assertEqual((source / filename).read_bytes(), (root / 'assets/sounds' / filename).read_bytes())

    def test_pending_rejected_duplicate_and_historical_records_do_not_chime(self):
        tracker = SoundTracker()
        source = object()
        self.assertEqual(tracker.observe(source, {'old': '全部成交'}, {'oldfill'}), [])
        self.assertEqual(tracker.observe(source, {'old': '全部成交', 'new': '待回报'}, {'oldfill'}), [])
        self.assertEqual(tracker.observe(source, {'old': '全部成交', 'new': '已拒绝'}, {'oldfill'}), [])
        self.assertEqual(tracker.observe(source, {'old': '全部成交', 'new': '已拒绝'}, {'oldfill'}), [])

    def test_accepted_fill_cancel_and_open_close_fills_share_sound(self):
        tracker = SoundTracker()
        source = object()
        tracker.observe(source, {}, set())
        orders = {'a': '已受理'}
        self.assertEqual(tracker.observe(source, orders, set()), ['accepted'])
        self.assertEqual(tracker.observe(source, orders, set()), [])
        orders['a'] = '部分成交'
        self.assertEqual(tracker.observe(source, orders, {'openfill'}), ['fill'])
        orders['a'] = '已撤单'
        self.assertEqual(tracker.observe(source, orders, {'openfill'}), ['canceled'])
        self.assertEqual(tracker.observe(source, orders, {'openfill'}), [])
        self.assertEqual(tracker.observe(source, orders, {'openfill', 'closefill'}), ['fill'])

    def test_reconnect_query_baseline_suppresses_replaying_sounds(self):
        tracker = SoundTracker()
        source = object()
        tracker.observe(source, {}, set())
        self.assertEqual(tracker.observe(source, {'a': '未成交'}, {'historical'}, audible=False), [])
        self.assertEqual(tracker.observe(source, {'a': '未成交'}, {'historical'}), [])
        self.assertEqual(tracker.observe(object(), {'a': '已撤单'}, {'historical'}), [])

    def test_tcp_failures_explain_stage_without_echoing_sensitive_os_messages(self):
        cases = ((PermissionError(errno.EPERM, 'sensitive'), '系统拒绝'), (TimeoutError(), 'TCP 连接超时'), (ConnectionRefusedError(errno.ECONNREFUSED, 'sensitive'), '拒绝 TCP'), (socket.gaierror(-2, 'sensitive'), '域名解析失败'))
        for error, expected in cases:
            with self.subTest(expected=expected), patch('pytrade.transport.socket.create_connection', side_effect=error):
                message = check_market_transport('tcp://127.0.0.1:1234')
                self.assertIn(expected, message)
                self.assertNotIn('sensitive', message)

    def test_worker_reports_network_denial_without_loading_sdk_or_sending_credentials(self):
        from pytrade.ctp_worker import worker_main
        from pytrade.external import ConnectionConfig
        outgoing = queue.Queue()
        config = ConnectionConfig(md_front='tcp://127.0.0.1:1234', md_anonymous=True)
        with patch('pytrade.transport.check_market_transport', return_value='系统拒绝行情网络连接'):
            worker_main(config, queue.Queue(), outgoing, '/tmp/unused-pytrade-flow', '/tmp/unused-locale')
        self.assertEqual(outgoing.get_nowait(), {'kind': 'fatal', 'message': '系统拒绝行情网络连接'})
        self.assertTrue(outgoing.empty())


if __name__ == '__main__':
    unittest.main()
