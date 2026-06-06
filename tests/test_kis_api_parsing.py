import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kis_api import KISAPI


class TestKISAPIParsing(unittest.TestCase):
    def test_parse_us_daily_rows_from_output2(self):
        resp = {
            "rt_cd": "0",
            "output2": [
                {"xymd": "20260425", "open": "101", "high": "110", "low": "99", "clos": "108", "evol": "1200"},
                {"xymd": "20260424", "open": "95", "high": "102", "low": "93", "clos": "100", "evol": "900"},
            ],
        }

        rows = KISAPI._parse_us_daily_rows(resp, period=30)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "20260424")
        self.assertEqual(rows[1]["close"], 108.0)

    def test_parse_us_daily_rows_from_output1(self):
        resp = {
            "rt_cd": "0",
            "output1": [
                {"date": "20260425", "open": "201", "high": "205", "low": "198", "close": "203", "volume": "777"},
            ],
        }

        rows = KISAPI._parse_us_daily_rows(resp, period=10)

        self.assertEqual(rows, [{
            "date": "20260425",
            "open": 201.0,
            "high": 205.0,
            "low": 198.0,
            "close": 203.0,
            "volume": 777,
        }])

    def test_parse_us_daily_rows_from_nested_output1_dict(self):
        resp = {
            "rt_cd": "0",
            "output1": {
                "symbol": "AMZN",
                "chart_data": {
                    "items": [
                        {"xymd": "20260425", "open": "11", "high": "12", "low": "10", "clos": "11.5", "evol": "50"},
                        {"xymd": "20260424", "open": "10", "high": "11", "low": "9", "clos": "10.5", "evol": "40"},
                    ]
                },
            },
        }

        rows = KISAPI._parse_us_daily_rows(resp, period=10)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "20260424")
        self.assertEqual(rows[1]["close"], 11.5)

    def test_parse_us_balance_payload_primary_keys(self):
        data = {
            "output1": [{"ovrs_pdno": "NVDA", "ovrs_cblc_qty": "2"}],
            "output2": [{
                "tot_evlu_amt": "1234.56",
                "frcr_dncl_one_amt": "900.12",
                "tot_evlu_pfls_amt": "44.5",
            }],
        }

        parsed = KISAPI._parse_us_balance_payload(data)

        self.assertEqual(parsed["total_usd"], 1234.56)
        self.assertEqual(parsed["available_usd"], 900.12)
        self.assertEqual(parsed["total_pnl_usd"], 44.5)
        self.assertEqual(len(parsed["stocks"]), 1)

    def test_parse_us_balance_payload_fallback_keys(self):
        data = {
            "output1": [],
            "output2": [{
                "tot_asst_amt": "5000",
                "frcr_buy_mgn_amt": "321.5",
                "ovrs_tot_pfls": "12.7",
            }],
        }

        parsed = KISAPI._parse_us_balance_payload(data)

        self.assertEqual(parsed["total_usd"], 5000.0)
        self.assertEqual(parsed["available_usd"], 321.5)
        self.assertEqual(parsed["total_pnl_usd"], 12.7)

    def test_parse_us_balance_payload_unsupported_status(self):
        data = {
            "msg_cd": "OPSQ0002",
            "msg1": "없는 서비스 코드 입니다",
            "output1": [],
            "output2": [],
        }

        parsed = KISAPI._parse_us_balance_payload(data)

        self.assertEqual(parsed["balance_status"], "unsupported")
        self.assertEqual(parsed["status_code"], "OPSQ0002")

    def test_access_token_is_shared_between_instances(self):
        original_token = KISAPI._shared_access_token
        original_expires = KISAPI._shared_token_expires
        try:
            KISAPI._shared_access_token = "TOKEN"
            KISAPI._shared_token_expires = 999.0
            with patch("kis_api.time.time", return_value=100.0), \
                 patch("kis_api.requests.post") as post_mock:
                first = KISAPI()
                second = KISAPI()
                self.assertEqual(first._get_access_token(), "TOKEN")
                self.assertEqual(second._get_access_token(), "TOKEN")
            post_mock.assert_not_called()
        finally:
            KISAPI._shared_access_token = original_token
            KISAPI._shared_token_expires = original_expires

    def test_us_exchange_map_includes_novo_nordisk_adr(self):
        self.assertEqual(KISAPI._resolve_us_exchange("NVO"), "NYS")

    def test_us_daily_empty_success_response_is_not_retried(self):
        api = KISAPI()
        api._headers = lambda tr_id: {}
        calls = []

        def fake_request(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "ok": True,
                "data": {
                    "rt_cd": "0",
                    "msg_cd": "MCA00000",
                    "msg1": "정상처리 되었습니다.",
                    "output1": {"nrec": "", "rsym": "", "zdiv": ""},
                    "output2": [],
                },
            }

        api._request_with_retry = fake_request

        with patch("kis_api.time.sleep") as sleep_mock:
            rows = api.get_us_stock_daily("WMT", period=30)

        self.assertEqual(rows, [])
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_default_global_throttle_interval_is_conservative_for_vts(self):
        self.assertGreaterEqual(KISAPI._min_request_interval, 1.0)

    def test_global_throttle_is_shared_between_instances(self):
        original_last = KISAPI._last_request_at
        original_min = KISAPI._min_request_interval
        original_path = KISAPI._throttle_state_path
        try:
            KISAPI._last_request_at = 100.0
            KISAPI._min_request_interval = 0.5
            with tempfile.TemporaryDirectory() as tmpdir:
                KISAPI._throttle_state_path = Path(tmpdir) / "kis_throttle.json"
                now_values = iter([100.2, 100.7])

                with patch("kis_api.time.monotonic", side_effect=lambda: next(now_values)), \
                     patch("kis_api.time.sleep") as sleep_mock:
                    second = KISAPI()
                    second._throttle_request("second")

            sleep_mock.assert_called_once()
            self.assertAlmostEqual(sleep_mock.call_args.args[0], 0.3, places=6)
            self.assertEqual(KISAPI._last_request_at, 100.7)
        finally:
            KISAPI._last_request_at = original_last
            KISAPI._min_request_interval = original_min
            KISAPI._throttle_state_path = original_path

    def test_global_throttle_is_shared_across_process_state_file(self):
        original_last = KISAPI._last_request_at
        original_min = KISAPI._min_request_interval
        original_path = KISAPI._throttle_state_path
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                KISAPI._throttle_state_path = Path(tmpdir) / "kis_throttle.json"
                KISAPI._min_request_interval = 1.0

                with patch("kis_api.time.monotonic", return_value=100.0), \
                     patch("kis_api.time.sleep"):
                    KISAPI._last_request_at = 0.0
                    first = KISAPI()
                    first._throttle_request("first")

                # 다른 프로세스처럼 메모리 timestamp가 비어도 파일 timestamp를 존중해야 한다.
                KISAPI._last_request_at = 0.0
                now_values = iter([100.25, 101.0])
                with patch("kis_api.time.monotonic", side_effect=lambda: next(now_values)), \
                     patch("kis_api.time.sleep") as sleep_mock:
                    second = KISAPI()
                    second._throttle_request("second")

            sleep_mock.assert_called_once()
            self.assertAlmostEqual(sleep_mock.call_args.args[0], 0.75, places=6)
            self.assertEqual(KISAPI._last_request_at, 101.0)
        finally:
            KISAPI._last_request_at = original_last
            KISAPI._min_request_interval = original_min
            KISAPI._throttle_state_path = original_path

    def test_global_throttle_respects_persisted_rate_limit_pause(self):
        original_last = KISAPI._last_request_at
        original_min = KISAPI._min_request_interval
        original_path = KISAPI._throttle_state_path
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                state_path = Path(tmpdir) / "kis_throttle.json"
                state_path.write_text('{"last_request_at": 100.0, "pause_until": 105.0}', encoding="utf-8")
                KISAPI._throttle_state_path = state_path
                KISAPI._last_request_at = 0.0
                KISAPI._min_request_interval = 0.5
                now_values = iter([101.0, 105.0])

                with patch("kis_api.time.monotonic", side_effect=lambda: next(now_values)), \
                     patch("kis_api.time.sleep") as sleep_mock:
                    api = KISAPI()
                    api._throttle_request("after-rate-limit")

            sleep_mock.assert_called_once()
            self.assertAlmostEqual(sleep_mock.call_args.args[0], 4.0, places=6)
            self.assertEqual(KISAPI._last_request_at, 105.0)
        finally:
            KISAPI._last_request_at = original_last
            KISAPI._min_request_interval = original_min
            KISAPI._throttle_state_path = original_path


    def test_global_throttle_ignores_stale_pause_until_from_previous_boot(self):
        """재부팅 후 monotonic 클럭 리셋 시 pause_until이 비현실적으로 크면 무시한다."""
        original_last = KISAPI._last_request_at
        original_min = KISAPI._min_request_interval
        original_path = KISAPI._throttle_state_path
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                state_path = Path(tmpdir) / "kis_throttle.json"
                # 재부팅 전 저장된 pause_until=7451, monotonic 리셋 후 now≈0
                state_path.write_text(
                    '{"last_request_at": 7446.0, "pause_until": 7451.0}',
                    encoding="utf-8",
                )
                KISAPI._throttle_state_path = state_path
                KISAPI._last_request_at = 0.0
                KISAPI._min_request_interval = 0.5
                now_values = iter([0.0, 1.0])  # monotonic이 리셋됨

                with patch("kis_api.time.monotonic", side_effect=lambda: next(now_values)), \
                     patch("kis_api.time.sleep") as sleep_mock:
                    api = KISAPI()
                    api._throttle_request("stale-pause")

                # sleep이 호출되어서는 안 됨 (stale pause_until 무시)
                self.assertEqual(sleep_mock.call_count, 0,
                                 "stale pause_until로 2시간 sleep하면 안 됨")
                # throttle state 파일의 pause_until이 리셋되었는지 확인
                final_state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertNotIn("pause_until", final_state,
                                 "stale pause_until이 파일에 남아있으면 안 됨")
                self.assertIsInstance(final_state.get("last_request_at"), float,
                                      "last_request_at이 파일에 저장되어야 함")
        finally:
            KISAPI._last_request_at = original_last
            KISAPI._min_request_interval = original_min
            KISAPI._throttle_state_path = original_path


if __name__ == "__main__":
    unittest.main()
