import unittest
from decimal import Decimal
from types import SimpleNamespace

from pytrade.risk import RiskBook, RiskLimits, position_id
from pytrade.stops import StopManager


def position(quantity=5):
    return dict(ExchangeID='SHFE', InstrumentID='au2612', PosiDirection='2', PositionDate='1', HedgeFlag='1', Position=quantity)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.book = RiskBook()
        self.limits = RiskLimits(10, 5, 2, '100')
        self.account = dict(CurrencyID='CNY', Balance=1000, Deposit=0, Withdraw=0)
        self.book.equity([self.account], '20261030', self.limits)

    def test_account_and_group_pending_reservations(self):
        row = position(5)
        self.book.assign(row, 1, 3)
        self.book.check_open(self.limits, [row], [], 2, 1)
        with self.assertRaises(ValueError):
            self.book.check_open(self.limits, [row], [dict(Group=1, VolumeTotalOriginal=2, VolumeTraded=0)], 1, 1)
        with self.assertRaises(ValueError):
            self.book.check_open(self.limits, [row], [dict(Group=2, VolumeTotalOriginal=5, VolumeTraded=0)], 1, 1)
        with self.assertRaises(ValueError):
            self.book.assign(row, 2, 3)

    def test_equity_adjusts_cash_flows_and_loss_latches(self):
        self.book.equity([self.account | dict(Balance=1400, Deposit=500)], '20261030', self.limits)
        self.assertEqual(self.book.pnl, Decimal('-100'))
        self.assertTrue(self.book.loss_locked)
        restored = RiskBook(self.book.state())
        restored.equity([self.account | dict(Balance=2000)], '20261030', self.limits)
        with self.assertRaises(ValueError):
            restored.check_open(self.limits, [], [], 1, 1)
        restored.equity([self.account], '20261102', self.limits)
        self.assertFalse(restored.loss_locked)
        restored.check_open(self.limits, [], [], 1, 1)

    def test_missing_cash_fields_and_currency_ambiguity_block(self):
        for accounts in ([dict(Balance=1000)], [self.account, self.account]):
            self.book.equity(accounts, '20261030', self.limits)
            with self.assertRaises(ValueError):
                self.book.check_open(self.limits, [], [], 1, 1)

    def test_cancel_count_restored_and_only_unsent_released(self):
        self.book.reserve_cancel(self.limits, 'a')
        self.book.reserve_cancel(self.limits, 'b')
        restored = RiskBook(self.book.state())
        with self.assertRaises(ValueError):
            restored.reserve_cancel(self.limits, 'c')
        restored.unsent_cancel('a')
        restored.unsent_cancel('a')
        self.assertEqual(restored.cancel_count, 1)
        restored.reserve_cancel(self.limits, 'c')

    def test_fill_allocation_idempotent_and_unknown_closes_clear(self):
        row = position(2)
        order = dict(TradingDay='20261030', ExchangeID='SHFE', InstrumentID='au2612', OrderRef='1', OrderSysID='sys', Group=2)
        trade = order | dict(TradeID='t', Direction='0', OffsetFlag='0', Volume=2)
        self.book.reconcile([row], {'t': trade}, {'o': order})
        self.book.reconcile([row], {'t': trade}, {'o': order})
        self.assertEqual(self.book.held(2), 2)
        self.book.reconcile([position(1)], {'t': trade}, {'o': order})
        self.assertEqual(self.book.held(2), 0)

    def test_hedged_trade_cannot_be_allocated_to_speculation_position(self):
        speculative = position(3)
        hedged = speculative | dict(HedgeFlag='3', Position=7)
        order = dict(TradingDay='20261030', ExchangeID='SHFE', InstrumentID='au2612', OrderSysID='hedged', Group=2, CombHedgeFlag='3')
        trade = order | dict(Direction='0', OffsetFlag='0', HedgeFlag='3', Volume=3)
        self.book.reconcile([speculative, hedged], {'hedged': trade}, {'order': order})
        self.assertEqual(self.book.held(2), 0)

    def test_external_manual_orders_count_against_account_limit(self):
        with self.assertRaises(ValueError):
            self.book.check_open(self.limits, [position(5), position(4) | dict(HedgeFlag='3')], [dict(VolumeTotalOriginal=1, VolumeTraded=0)], 1, 1)

    def test_invalid_persisted_limits_and_allocations_rejected(self):
        for state in (dict(cancel_count=-1), dict(allocations={'bad': {}}), dict(baseline='NaN')):
            with self.assertRaises(ValueError):
                RiskBook(state)
        for loss in ('NaN', 'bad', '-1'):
            with self.assertRaises(ValueError):
                RiskLimits(1, 1, 1, loss).validate()

    def test_trading_day_rolls_shfe_group_allocation_to_yesterday(self):
        row = position(3)
        self.book.assign(row, 2, 3)
        self.book.set_day('20261102')
        self.assertEqual(self.book.held(2, 'SHFE.au2612', '2', '1'), 0)
        self.assertEqual(self.book.held(2, 'SHFE.au2612', '2', '2'), 3)
        self.book.reconcile([row | dict(PositionDate='2')], {}, {})
        self.assertEqual(self.book.held(2), 3)


class Broker:
    def __init__(self):
        self.quote = SimpleNamespace(last=100, bids=((99, 1),), asks=((101, 1),))
        self.held = 3
        self.ready = True
        self.conflicts = []
        self.canceled = []
        self.sent = []
        self.status = 'working'
        self.changes = 0
        self.cancel_error = False

    def stop_ready(self, key):
        if not self.ready:
            raise ValueError('行情过期')

    def stop_quote(self, key):
        return self.quote

    def stop_held(self, *args):
        return self.held

    def stop_changed(self):
        self.changes += 1

    def stop_conflicts(self, stop):
        return self.conflicts

    def stop_cancel_order(self, identifier):
        if self.cancel_error:
            raise ValueError('撤单失败')
        if identifier not in self.canceled:
            self.canceled.append(identifier)

    def stop_submit(self, stop, ticks):
        self.sent.append((stop.quantity, ticks))
        return 'order1'

    def stop_order_state(self, identifier):
        return self.status


class StopsTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker()
        self.manager = StopManager(self.broker)
        self.stop = self.manager.add('SHFE.au2612', '买', '平今', 99, 2, 1)

    def test_trigger_once_and_wait_for_actual_fill(self):
        self.manager.arm(self.stop.id)
        self.manager.poll()
        self.assertEqual(self.broker.sent, [])
        self.broker.quote.last = 99
        self.manager.poll()
        self.manager.poll()
        self.assertEqual(self.broker.sent, [(2, 99)])
        self.assertEqual(self.stop.state, 'working')
        self.broker.status = 'filled'
        self.manager.poll()
        self.assertEqual(self.stop.state, 'filled')

    def test_conflicting_close_waits_for_reconciliation(self):
        self.manager.arm(self.stop.id)
        self.broker.quote.last = 98
        self.broker.conflicts = ['close1']
        self.manager.poll()
        self.assertEqual(self.broker.canceled, ['close1'])
        self.assertFalse(self.broker.sent)
        self.broker.conflicts = []
        self.broker.ready = False
        self.manager.poll()
        self.assertFalse(self.broker.sent)
        self.broker.ready = True
        self.broker.quote.last = 102
        self.manager.poll()
        self.assertEqual(len(self.broker.sent), 1)

    def test_stale_missing_book_and_cancel_failure_do_not_send(self):
        self.manager.arm(self.stop.id)
        self.broker.quote.last = 98
        self.broker.ready = False
        self.manager.poll()
        self.assertFalse(self.stop.triggered)
        self.broker.ready = True
        self.broker.quote.bids = ()
        self.manager.poll()
        self.assertTrue(self.stop.triggered)
        self.assertFalse(self.broker.sent)
        self.broker.conflicts = ['close1']
        self.broker.cancel_error = True
        self.manager.poll()
        self.assertFalse(self.broker.sent)
        self.assertIn('撤单失败', self.stop.message)

    def test_restart_requires_review_and_never_duplicates_existing_order(self):
        self.manager.arm(self.stop.id)
        self.broker.quote.last = 98
        self.manager.poll()
        restored = StopManager(self.broker, self.manager.records())
        restored.poll()
        self.assertEqual(restored.stops[self.stop.id].state, 'paused')
        with self.assertRaises(ValueError):
            restored.arm(self.stop.id)
        self.assertEqual(len(self.broker.sent), 1)

    def test_reservations_apply_across_local_and_server_modes(self):
        with self.assertRaises(ValueError):
            self.manager.add('SHFE.au2612', '买', '平今', 99, 2, 1)
        with self.assertRaises(ValueError):
            self.manager.add('SHFE.au2612', '买', '平今', 99, 1, 1, 'server')
        self.manager.cancel(self.stop.id)
        self.manager.add('SHFE.au2612', '买', '平今', 99, 3, 1)

    def test_rejected_close_not_retried(self):
        self.manager.arm(self.stop.id)
        self.broker.quote.last = 98
        self.manager.poll()
        self.broker.status = 'failed'
        self.manager.poll()
        self.manager.poll()
        self.assertEqual(len(self.broker.sent), 1)
        self.assertEqual(self.stop.state, 'failed')

    def test_observed_extrema_latch_trigger_until_trading_is_ready(self):
        self.manager.arm(self.stop.id)
        self.broker.ready = False
        self.manager.observe('SHFE.au2612', 98, 102)
        self.manager.poll()
        self.assertTrue(self.stop.triggered)
        self.assertFalse(self.broker.sent)
        self.broker.ready = True
        self.broker.quote.last = 103
        self.manager.poll()
        self.assertEqual(self.broker.sent, [(2, 99)])


class HostedProvider:
    def __init__(self):
        self.creates = 0
        self.timeout = False
        self.accepted = False
        self.client_id = ''
        self.filled = False
        self.canceled = False

    def capabilities(self):
        return dict(supported=True)

    def create(self, request):
        self.creates += 1
        self.client_id = request['id']
        if self.timeout:
            raise ValueError('timeout with unknown result')
        return 'server1'

    def query(self, reference):
        return dict(reference='server1', client_id=self.client_id, accepted=self.accepted, filled=self.filled, canceled=self.canceled, order_id='order1')

    def cancel(self, reference):
        self.canceled = True


class HostedStopTests(unittest.TestCase):
    def test_no_hosted_label_until_server_query_confirms(self):
        broker, provider = Broker(), HostedProvider()
        manager = StopManager(broker, provider=provider)
        stop = manager.add('SHFE.au2612', '买', '平今', 99, 2, 1, 'server')
        manager.arm(stop.id)
        self.assertEqual(stop.state, 'submitted')
        manager.poll()
        self.assertEqual(stop.state, 'submitted')
        provider.accepted = True
        manager.poll()
        self.assertEqual(stop.state, 'hosted')
        provider.filled = True
        manager.poll()
        self.assertEqual(stop.state, 'hosted')
        broker.status = 'filled'
        manager.poll()
        self.assertEqual(stop.state, 'filled')
        self.assertFalse(broker.sent)

    def test_timeout_does_not_create_another_server_stop_after_restart(self):
        broker, provider = Broker(), HostedProvider()
        manager = StopManager(broker, provider=provider)
        stop = manager.add('SHFE.au2612', '买', '平今', 99, 2, 1, 'server')
        provider.timeout = True
        with self.assertRaises(ValueError):
            manager.arm(stop.id)
        restored = StopManager(broker, manager.records(), provider)
        with self.assertRaises(ValueError):
            restored.arm(stop.id)
        self.assertEqual(provider.creates, 1)
        provider.accepted = True
        restored.arm(stop.id)
        self.assertEqual(provider.creates, 1)
        restored.cancel(stop.id)
        restored.poll()
        self.assertEqual(restored.stops[stop.id].state, 'canceled')

    def test_terminal_failed_close_can_release_stop_reservation_after_review(self):
        broker = Broker()
        manager = StopManager(broker)
        stop = manager.add('SHFE.au2612', '买', '平今', 99, 2, 1)
        manager.arm(stop.id)
        broker.quote.last = 98
        manager.poll()
        with self.assertRaises(ValueError):
            manager.cancel(stop.id)
        broker.status = 'failed'
        manager.poll()
        manager.cancel(stop.id)
        self.assertEqual(stop.state, 'canceled')
        manager.add('SHFE.au2612', '买', '平今', 99, 3, 1)
