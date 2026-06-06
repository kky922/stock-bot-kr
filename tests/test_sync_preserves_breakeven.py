"""TDD: Reconciliation must preserve breakeven protection.

시나리오:
1. 모니터 에이전트가 slot.breakeven_protect=True로 설정 (SL=진입가)
2. 다음 파이프라인 사이클에서 _sync_positions_from_account 실행
3. KIS가 동일 수량/평단 반환 → sync가 slot을 업데이트하지 않아야 함
4. KIS가 다른 수량/평단 반환 → sync가 수량은 업데이트하되 breakeven SL은 보존해야 함
"""

import unittest
from unittest.mock import patch

import run_agents


class FakeStore:
    def __init__(self, slots=None):
        self.slots = slots or {}
        self.saved = {}
        self.removed = []
        self.appended = []

    def load_all_slots(self, market=None):
        if market is None:
            return dict(self.slots)
        return {sid: p for sid, p in self.slots.items()
                if p.get("market", "KR") == market}

    def load_slot(self, slot_id):
        return self.slots.get(slot_id)

    def save_slot(self, slot_id, position):
        self.saved[slot_id] = position
        if position is None:
            self.removed.append(slot_id)
            self.slots.pop(slot_id, None)
        else:
            self.slots[slot_id] = position

    def append_trade(self, trade):
        self.appended.append(trade)

    def append_recommendation(self, rec):
        self.appended.append(rec)

    def record_api_error(self, *args, **kwargs):
        pass


class FakeKIS:
    mode = "virtual"
    account_no = "12345678"
    account_product = "01"

    def __init__(self, stocks=None):
        self.stocks = stocks or []
        self.balance = {"stocks": self.stocks}

    def get_balance(self):
        return self.balance

    def get_us_balance(self):
        return {"stocks": []}

    def get_exchange_rate(self):
        return 1350.0


class TestSyncPreservesBreakeven(unittest.TestCase):
    """Reconciliation must NOT reset SL/TP when breakeven_protect is active."""

    def test_sync_preserves_breakeven_sl_on_same_quantity_and_price(self):
        """동일 수량/평단이면 slot을 전혀 수정하지 않음 (breakeven SL 보존)."""
        store = FakeStore(slots={
            "KR_005930": {
                "market": "KR",
                "code": "005930",
                "name": "삼성전자",
                "entry_price": 293500.0,
                "quantity": 2,
                "total_quantity": 2,
                "stop_loss_price": 293500,  # breakeven (entry price)
                "take_profit_price": 322850,
                "breakeven_protect": True,
                "highest_price": 300000.0,
                "source": "manual",
            },
        })
        kis = FakeKIS(stocks=[
            {
                "code": "005930",
                "name": "삼성전자",
                "avg_price": 293500.0,
                "quantity": 2,
            }
        ])

        run_agents._sync_positions_from_account(kis, store)

        saved = store.slots["KR_005930"]
        self.assertTrue(saved.get("breakeven_protect"),
                        "breakeven_protect가 sync 후에도 유지되어야 함")
        self.assertEqual(saved.get("stop_loss_price"), 293500,
                         "breakeven SL이 sync 후에도 유지되어야 함")

    def test_sync_preserves_breakeven_sl_on_price_change(self):
        """가격/수량이 변경되어도 breakeven SL은 유지되어야 함."""
        store = FakeStore(slots={
            "KR_005930": {
                "market": "KR",
                "code": "005930",
                "name": "삼성전자",
                "entry_price": 293500.0,
                "quantity": 2,
                "total_quantity": 2,
                "stop_loss_price": 293500,  # breakeven
                "take_profit_price": 322850,
                "breakeven_protect": True,
                "highest_price": 300000.0,
                "source": "manual",
            },
        })
        kis = FakeKIS(stocks=[
            {
                "code": "005930",
                "name": "삼성전자",
                "avg_price": 293500.0,
                "quantity": 3,  # 수량 증가!
            }
        ])

        run_agents._sync_positions_from_account(kis, store)

        saved = store.slots["KR_005930"]
        self.assertEqual(saved.get("quantity"), 3,
                         "수량은 업데이트되어야 함")
        self.assertTrue(saved.get("breakeven_protect"),
                        "breakeven_protect가 sync 후에도 유지되어야 함")
        self.assertEqual(saved.get("stop_loss_price"), 293500,
                         "breakeven SL이 sync 후에도 유지되어야 함 (재계산 금지)")

    def test_sync_overwrites_sl_when_no_breakeven(self):
        """breakeven_protect가 없으면 정상적으로 SL/TP 재계산."""
        store = FakeStore(slots={
            "KR_005930": {
                "market": "KR",
                "code": "005930",
                "name": "삼성전자",
                "entry_price": 293500.0,
                "quantity": 2,
            },
        })
        kis = FakeKIS(stocks=[
            {
                "code": "005930",
                "name": "삼성전자",
                "avg_price": 293500.0,
                "quantity": 3,  # 수량 변경
            }
        ])

        run_agents._sync_positions_from_account(kis, store)

        saved = store.slots["KR_005930"]
        expected_sl = round(293500.0 * (1 - 7.0 / 100))
        self.assertEqual(saved.get("stop_loss_price"), expected_sl,
                         "breakeven 없으면 SL 재계산되어야 함")
        self.assertEqual(saved.get("quantity"), 3)


if __name__ == "__main__":
    unittest.main()
