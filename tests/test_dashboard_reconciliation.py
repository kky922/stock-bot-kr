import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import dashboard
import run_agents


class FakeStore:
    def __init__(self, trades=None, slots=None):
        self.trades = trades or []
        self.slots = slots or {}
        self.removed = []
        self.appended = []
        self.saved = {}

    def get_trades(self, limit=50):
        return self.trades[-limit:]

    def load_all_slots(self, market=None):
        if market is None:
            return dict(self.slots)
        return {
            slot_id: position
            for slot_id, position in self.slots.items()
            if position.get("market", "KR") == market
        }

    def load_slot(self, slot_id):
        return self.slots.get(slot_id)

    def save_slot(self, slot_id, position):
        self.saved[slot_id] = position
        if position is None:
            self.removed.append(slot_id)
            self.slots.pop(slot_id, None)
        else:
            self.slots[slot_id] = position

    def get_open_slot_count(self, market):
        return sum(
            1 for p in self.slots.values()
            if p.get("market", "KR") == market
        )

    def find_slot_by_code(self, market, stock_code):
        for slot_id, pos in self.slots.items():
            if pos.get("market", "KR") == market and pos.get("code") == stock_code:
                return pos
        return None

    def get_cooldown(self, slot_id):
        return None

    def get_market_state(self, market):
        return {}

    def safe_save(self, name, data):
        self.saved[name] = data

    def safe_load(self, name):
        return self.saved.get(name, {})

    def load_all_positions(self, market):
        return list(self.load_all_slots(market).values())

    def record_api_error(self, market, error_code, message=""):
        return {}

    def append_trade(self, trade):
        self.appended.append(trade)

    def append_recommendation(self, recommendation):
        self.appended.append(recommendation)


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


class FakeOrchestrator:
    def __init__(self, store, actionable):
        self.store = store
        self.actionable = actionable

    def run_scan_pipeline(self, kis_api=None):
        return list(self.actionable)

    def get_actionable_signals(self):
        return list(self.actionable)


class RaisingExecutor:
    def execute_slot_buy(self, slot_id, decision):
        raise AssertionError("execute_slot_buy should not be called")


class FailedBalanceKIS:
    def get_balance(self):
        return {"balance_status": "failed", "stocks": []}


class DashboardReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.old_store = dashboard._store
        self.old_kis = dashboard._kis
        self.old_us_enabled = dashboard.config.US_STOCK_ENABLED

    def tearDown(self):
        dashboard._store = self.old_store
        dashboard._kis = self.old_kis
        dashboard.config.US_STOCK_ENABLED = self.old_us_enabled

    def test_trades_endpoint_reads_agent_store_latest_first(self):
        dashboard._store = FakeStore(trades=[
            {
                "timestamp": "2026-04-27T00:10:00+00:00",
                "action": "sell",
                "name": "old",
                "quantity": 1,
                "sell_price": 100,
            },
            {
                "timestamp": "2026-04-29T00:10:00+00:00",
                "action": "buy",
                "name": "new",
                "quantity": 2,
                "entry_price": 200,
            },
        ])

        data = dashboard.app.test_client().get("/api/trades?limit=10").get_json()

        self.assertTrue(data["success"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["trades"][0]["name"], "new")
        self.assertEqual(data["trades"][0]["price"], 200)

    def test_sell_price_is_normalized_to_price(self):
        normalized = dashboard.normalize_trade({
            "action": "sell",
            "name": "SK하이닉스",
            "quantity": 2,
            "sell_price": 1270000,
        })

        self.assertEqual(normalized["side"], "sell")
        self.assertEqual(normalized["price"], 1270000)

    def test_positions_endpoint_marks_matched_and_bot_only_slots(self):
        dashboard.config.US_STOCK_ENABLED = False
        dashboard._kis = FakeKIS(stocks=[
            {
                "code": "005930",
                "name": "삼성전자",
                "quantity": 11,
                "avg_price": 221500,
                "current_price": 222000,
                "pnl": 5500,
                "pnl_rate": 0.23,
            }
        ])
        dashboard._store = FakeStore(slots={
            "KR_005930": {"market": "KR", "code": "005930", "name": "삼성전자", "entry_price": 221500, "quantity": 11},
            "KR_000660": {"market": "KR", "code": "000660", "name": "SK하이닉스", "entry_price": 1205000, "quantity": 2},
        })

        data = dashboard.app.test_client().get("/api/positions").get_json()
        sources = {position["code"]: position["source"] for position in data["positions"]}

        self.assertEqual(sources["005930"], "matched")
        self.assertEqual(sources["000660"], "bot_slot")

    def test_positions_endpoint_marks_quantity_mismatch(self):
        dashboard.config.US_STOCK_ENABLED = False
        dashboard._kis = FakeKIS(stocks=[
            {
                "code": "005930",
                "name": "삼성전자",
                "quantity": 12,
                "avg_price": 220895.833,
                "current_price": 226000,
                "pnl": 61250,
                "pnl_rate": 2.31,
            }
        ])
        dashboard._store = FakeStore(slots={
            "KR_005930": {"market": "KR", "code": "005930", "name": "삼성전자", "entry_price": 221500, "quantity": 11},
        })

        data = dashboard.app.test_client().get("/api/positions").get_json()
        position = data["positions"][0]

        self.assertEqual(position["source"], "matched")
        self.assertTrue(position["quantity_mismatch"])
        self.assertEqual(position["slot_quantity"], 11)

    def test_sync_removes_slots_missing_from_account(self):
        store = FakeStore(slots={
            "KR_005930": {"market": "KR", "code": "005930", "name": "삼성전자", "entry_price": 221500, "quantity": 11},
            "KR_000660": {"market": "KR", "code": "000660", "name": "SK하이닉스", "entry_price": 1205000, "quantity": 2},
        })
        kis = FakeKIS(stocks=[
            {"code": "005930", "name": "삼성전자", "avg_price": 221500, "quantity": 11}
        ])

        run_agents._sync_positions_from_account(kis, store)

        self.assertNotIn("KR_005930", store.removed)
        self.assertIn("KR_000660", store.removed)
        self.assertEqual(store.appended[0]["action"], "reconcile_remove")
        self.assertEqual(store.appended[0]["code"], "000660")

    def test_sync_updates_existing_slot_quantity_from_account(self):
        store = FakeStore(slots={
            "KR_005930": {"market": "KR", "code": "005930", "name": "삼성전자", "entry_price": 221500, "quantity": 11},
        })
        kis = FakeKIS(stocks=[
            {"code": "005930", "name": "삼성전자", "avg_price": 220895.833, "quantity": 12}
        ])

        run_agents._sync_positions_from_account(kis, store)

        saved = store.slots["KR_005930"]
        self.assertEqual(saved["quantity"], 12)
        self.assertEqual(saved["total_quantity"], 12)
        self.assertEqual(saved["entry_price"], 220895.833)
        self.assertEqual(store.appended[0]["action"], "reconcile_update")

    def test_sync_does_not_remove_slots_when_balance_lookup_failed(self):
        store = FakeStore(slots={
            "KR_005930": {"market": "KR", "code": "005930", "name": "삼성전자", "entry_price": 221500, "quantity": 11},
        })

        run_agents._sync_positions_from_account(FailedBalanceKIS(), store)

        self.assertEqual(store.removed, [])
        self.assertEqual(store.appended, [])

    def test_overview_uses_total_eval_without_adding_cash_again(self):
        dashboard.config.US_STOCK_ENABLED = False
        kis = FakeKIS()
        kis.balance = {
            "balance_status": "ok",
            "total_deposit": 4285670,
            "cash": 4285670,
            "total_eval": 10435670,
            "total_assets": 10435670,
            "stock_eval": 6150000,
            "total_pnl": 13250,
            "stocks": [],
        }
        dashboard._kis = kis

        data = dashboard.app.test_client().get("/api/overview").get_json()

        self.assertTrue(data["success"])
        self.assertEqual(data["total"]["assets"], 10435670)
        self.assertEqual(data["kr"]["total_assets"], 10435670)
        self.assertEqual(data["kr"]["cash"], 4285670)
        self.assertEqual(data["kr"]["stock_eval"], 6150000)
        self.assertEqual(data["kr"]["pnl"], 13250)

    def test_calc_stock_eval_uses_current_price_times_quantity(self):
        stocks = [
            {"current_price": 226000, "quantity": 12},
            {"current_price": 1293000, "quantity": 1},
            {"current_price": 357500, "quantity": 6},
        ]

        self.assertEqual(dashboard.calc_stock_eval(stocks), 6150000)

    def test_telegram_dedupe_blocks_repeated_key(self):
        with TemporaryDirectory() as tmp:
            old_file = run_agents._TELEGRAM_DEDUPE_FILE
            run_agents._TELEGRAM_DEDUPE_FILE = Path(tmp) / "telegram_dedupe.json"
            try:
                self.assertTrue(run_agents._tg_should_send("same-key", cooldown_seconds=21600))
                self.assertFalse(run_agents._tg_should_send("same-key", cooldown_seconds=21600))
            finally:
                run_agents._TELEGRAM_DEDUPE_FILE = old_file

    def test_us_virtual_readiness_does_not_call_order_executor(self):
        old_mode = run_agents.config.KIS_MODE
        old_readiness = run_agents.config.US_READINESS_MODE
        old_us_enabled = run_agents.config.US_STOCK_ENABLED
        old_tg = run_agents._tg
        old_tg_dedupe = run_agents._tg_dedupe
        try:
            run_agents.config.KIS_MODE = "virtual"
            run_agents.config.US_READINESS_MODE = True
            run_agents.config.US_STOCK_ENABLED = True
            run_agents._tg = lambda text: None
            run_agents._tg_dedupe = lambda *args, **kwargs: None
            signal = {
                "market": "US",
                "code": "NVDA",
                "name": "NVDA",
                "entry_score": 80,
                "entry_verdict": "BUY",
                "risk_can_enter": True,
            }
            store = FakeStore()

            results = run_agents.run_pipeline_once(
                FakeKIS(),
                FakeOrchestrator(store, [signal]),
                RaisingExecutor(),
            )

            self.assertEqual(len(results), 1)
            self.assertEqual([r["outcome"] for r in store.appended], ["candidate"])
        finally:
            run_agents.config.KIS_MODE = old_mode
            run_agents.config.US_READINESS_MODE = old_readiness
            run_agents.config.US_STOCK_ENABLED = old_us_enabled
            run_agents._tg = old_tg
            run_agents._tg_dedupe = old_tg_dedupe

    def test_order_fail_is_recorded_for_penalty(self):
        store = FakeStore()
        signal = {"market": "US", "code": "NVDA", "name": "NVDA", "theme": "AI"}

        run_agents._record_order_fail(store, signal, "ERR", "fail message")

        self.assertEqual(store.appended[0]["outcome"], "order_fail")
        self.assertEqual(store.appended[0]["code"], "NVDA")


if __name__ == "__main__":
    unittest.main()
