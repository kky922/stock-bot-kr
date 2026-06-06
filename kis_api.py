"""
한국투자증권 (KIS) Open Trading API 연동 모듈.
인증, 시세조회, 주문실행을 담당합니다.
"""

import base64
import fcntl
import hashlib
import hmac
import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)


class KISAPI:
    """한국투자증권 Open API 클라이언트."""

    # KIS 호출 제한은 프로세스/앱키 단위로 걸리므로 KISAPI 인스턴스가 여러 개여도
    # HTTP 요청은 전역으로 최소 간격을 둔다. (메인 파이프라인 + 모니터 스레드 동시 호출 방지)
    _global_request_lock = threading.Lock()
    _last_request_at = 0.0
    _min_request_interval = float(getattr(config, "KIS_MIN_REQUEST_INTERVAL", 1.0))
    _throttle_state_path = config.DATA_DIR / "agents" / "kis_request_throttle.json"
    _token_lock = threading.Lock()
    _shared_access_token = ""
    _shared_token_expires = 0.0

    US_EXCHANGE_BY_SYMBOL = {
        # ── NYSE (NYS) ──
        "ABB": "NYS", "ALB": "NYS", "BA": "NYS", "BAC": "NYS", "BE": "NYS",
        "BRK.B": "NYS", "CCJ": "NYS", "CEG": "NYS", "CVX": "NYS", "DD": "NYS",
        "DIA": "NYS", "DIS": "NYS", "DOW": "NYS", "FDX": "NYS", "GD": "NYS",
        "GS": "NYS", "IBM": "NYS", "IWM": "NYS", "JNJ": "NYS", "JPM": "NYS",
        "LAC": "NYS", "LHX": "NYS", "LLY": "NYS", "LMT": "NYS", "LYV": "NYS",
        "MA": "NYS", "MATX": "NYS", "MP": "NYS", "NOC": "NYS", "NUE": "NYS",
        "NVO": "NYS", "PFE": "NYS", "QBTS": "NYS", "RDW": "NYS", "ROK": "NYS", "RTX": "NYS",
        "SMR": "NYS", "SNOW": "NYS", "SPY": "NYS", "SQM": "NYS", "T": "NYS",
        "TGT": "NYS", "TSM": "NYS", "UPS": "NYS", "V": "NYS", "VEEV": "NYS",
        "VRT": "NYS", "VST": "NYS", "VZ": "NYS", "WFC": "NYS", "WMT": "NYS",
        "WOLF": "NYS", "X": "NYS", "XOM": "NYS", "ZIM": "NYS",
        # ── NASDAQ (NAS) ──
        "AI": "NAS", "AMAT": "NAS", "AMD": "NAS", "AMGN": "NAS", "AMZN": "NAS",
        "ASML": "NAS", "ASTS": "NAS", "AVGO": "NAS", "BLNK": "NAS", "CDNS": "NAS",
        "CHPT": "NAS", "COST": "NAS", "CRM": "NAS", "EA": "NAS", "ENPH": "NAS",
        "FCEL": "NAS", "FSLR": "NAS", "GILD": "NAS", "GOOGL": "NAS", "ICLN": "NAS",
        "INTC": "NAS", "IONQ": "NAS", "ISRG": "NAS", "KLAC": "NAS", "LAZR": "NAS",
        "LCID": "NAS", "LRCX": "NAS", "MBLY": "NAS", "META": "NAS", "MRNA": "NAS",
        "MRVL": "NAS", "MSFT": "NAS", "MU": "NAS", "NFLX": "NAS", "NNE": "NAS",
        "NVDA": "NAS", "ON": "NAS", "PATH": "NAS", "PLTR": "NAS", "PLUG": "NAS",
        "QQQ": "NAS", "QRVO": "NAS", "REGN": "NAS", "RGTI": "NAS", "RIVN": "NAS",
        "RKLB": "NAS", "SATL": "NAS", "SEDG": "NAS", "SERV": "NAS", "SMCI": "NAS",
        "SMH": "NAS", "SNPS": "NAS", "SOXX": "NAS", "STLD": "NAS", "TAN": "NAS",
        "TDOC": "NAS", "TMUS": "NAS", "TSLA": "NAS", "TTWO": "NAS", "VRTX": "NAS",
    }

    def __init__(self):
        self.app_key = config.KIS_APP_KEY
        self.app_secret = config.KIS_APP_SECRET
        self.base_url = config.KIS_BASE_URL
        self.account_no = config.KIS_ACCOUNT_NO
        self.account_product = config.KIS_ACCOUNT_PRODUCT
        self.mode = config.KIS_MODE

        # 토큰 관리
        self._access_token: str = ""
        self._token_expires: float = 0.0

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        op_name: str,
        retries: int = 3,
        backoff: float = 0.7,
        **kwargs,
    ) -> Dict[str, Any]:
        """HTTP 요청 공통 재시도 래퍼.
        
        [Fix] KIS VTS가 HTTP 500을 반환할 때도 응답 본문(JSON 에러 메시지)을 
        읽을 수 있도록 raise_for_status() 대신 수동 상태코드 체크 방식으로 변경.
        """
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                self._throttle_request(op_name)
                resp = requests.request(method=method, url=url, timeout=10, **kwargs)
                
                # [Fix] raise_for_status() 대신 수동 체크 — 500 응답 본문도 읽을 수 있게
                if resp.status_code >= 400:
                    body = resp.text[:1000] if resp.text else ""
                    # 응답 본문에서 KIS 에러 메시지 추출 시도
                    try:
                        body_json = resp.json()
                        kis_msg = body_json.get("msg1", "")
                        kis_code = body_json.get("msg_cd", "")
                        if kis_msg:
                            body = f"{kis_code}: {kis_msg}" if kis_code else kis_msg
                    except (ValueError, KeyError):
                        pass
                    
                    # [Fix] 재시도 불가 에러 코드 목록
                    _no_retry_codes = {"IGW00002", "EGW00356", "EGW02006", "EGW02004"}
                    _rate_limit_codes = {"EGW00201", "EGW00131", "EGW00132"}
                    _token_expired_codes = {"EGW00123"}
                    _is_no_retry = kis_code in _no_retry_codes
                    _is_rate_limit = kis_code in _rate_limit_codes
                    _is_token_expired = kis_code in _token_expired_codes

                    last_error = {
                        "error_code": f"HTTP_{resp.status_code}",
                        "error_message": f"{resp.status_code} {resp.reason}: {body}",
                        "response_body": body,
                        "kis_code": kis_code,
                    }

                    if _is_token_expired:
                        logger.warning("🔄 %s: 접근토큰 만료 감지 — 토큰 재발급 후 재시도(%d/%d)", op_name, attempt, retries)
                        self._access_token = ""
                        self._token_expires = 0.0
                        # [Fix] 클래스 공유 캐시도 함께 초기화 — 안 하면 _get_access_token()이
                        # 만료된 shared token을 그대로 반환하여 tokenP 재발급을 건너뜀.
                        KISAPI._shared_access_token = ""
                        KISAPI._shared_token_expires = 0.0
                        try:
                            self._get_access_token()
                        except Exception as refresh_error:
                            logger.error("❌ %s: 토큰 재발급 실패: %s", op_name, refresh_error)
                            return {"ok": False, **last_error}
                        if attempt < retries:
                            time.sleep(0.5)
                            continue
                        return {"ok": False, **last_error}

                    if _is_no_retry:
                        # 재시도 불가 에러: 명확한 안내와 함께 즉시 실패
                        if kis_code == "IGW00002":
                            logger.error(
                                "🚫 %s: 계좌번호 불일치(IGW00002) — .env의 KIS_ACCOUNT_NO를 확인하세요. "
                                "모의투자=가상계좌, 실전=실계좌 여부 확인 필요. 즉시 중단.",
                                op_name,
                            )
                        elif kis_code == "EGW00356":
                            logger.error(
                                "🚫 %s: KIS 서버 라우팅 실패(EGW00356) — "
                                "모의투자(VTS) 서버에서 주문 API가 차단된 상태입니다. "
                                "해결: 1) KIS 모의투자 고객센터 문의 (1588-6611) "
                                "2) 실전 모드로 전환: ./switch_mode.sh real "
                                "(실전 APP_KEY/SECRET 필요)",
                                op_name,
                            )
                        elif kis_code in ("EGW02006", "EGW02004"):
                            logger.error(
                                "🚫 %s: API 키/모드 불일치(%s) — "
                                "모의투자 앱키로 실전 서버 접근 불가 또는 반대. "
                                ".env의 KIS_MODE와 KIS_APP_KEY 조합을 확인하세요.",
                                op_name, kis_code,
                            )
                        return {"ok": False, **last_error}

                    # [Fix] rate limit 에러는 더 긴 대기 후 재시도
                    if _is_rate_limit:
                        wait_time = 5.0 * attempt  # 5초, 10초, 15초...
                        self._record_rate_limit_pause(wait_time)
                        logger.warning(
                            "⏳ %s: Rate limit(%s) — %.1f초 대기 후 재시도(%d/%d)",
                            op_name, kis_code, wait_time, attempt, retries,
                        )
                        if attempt < retries:
                            time.sleep(wait_time)
                        continue

                    logger.warning(
                        "⚠️ %s 실패(%d/%d): %s | body=%s",
                        op_name,
                        attempt,
                        retries,
                        last_error["error_code"],
                        body[:300],
                    )
                    if attempt < retries:
                        time.sleep(backoff * attempt)
                    continue
                
                return {"ok": True, "data": resp.json(), "status_code": resp.status_code}
            except requests.RequestException as e:
                last_error = {"error_code": "REQUEST_ERROR", "error_message": str(e)}
                logger.warning("⚠️ %s 실패(%d/%d): %s", op_name, attempt, retries, e)
            except ValueError as e:
                last_error = {"error_code": "INVALID_JSON", "error_message": str(e)}
                logger.warning("⚠️ %s 실패(%d/%d): JSON 파싱 오류", op_name, attempt, retries)

            if attempt < retries:
                time.sleep(backoff * attempt)

        return {"ok": False, **(last_error or {"error_code": "UNKNOWN", "error_message": "unknown error"})}

    @classmethod
    def _throttle_request(cls, op_name: str = "") -> None:
        """프로세스/대시보드까지 포함해 모든 KIS REST 호출 사이에 최소 간격을 둔다."""
        with cls._global_request_lock:
            now = time.monotonic()

            def apply_wait(last_seen: float) -> None:
                nonlocal now
                # monotonic timestamp는 부팅 후 기준이므로 재부팅/깨진 파일로 미래값이 크면 무시한다.
                if last_seen > now + 60:
                    return
                wait_time = cls._min_request_interval - (now - last_seen)
                if wait_time > 0:
                    if op_name:
                        logger.debug("⏱️ KIS 요청 간격 조절: %s %.2f초 대기", op_name, wait_time)
                    time.sleep(wait_time)
                    now = time.monotonic()

            state_path = Path(cls._throttle_state_path)
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                with state_path.open("a+", encoding="utf-8") as fp:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                    fp.seek(0)
                    try:
                        state = json.loads(fp.read() or "{}")
                    except json.JSONDecodeError:
                        state = {}
                    persisted_last = cls._to_float(state.get("last_request_at"))
                    pause_until = cls._to_float(state.get("pause_until"))
                    last_seen = max(cls._last_request_at, persisted_last)
                    # [Fix] 재부팅/모노토닉 클럭 리셋으로 pause_until이 비현실적으로 크면 무시
                    if pause_until > now + 300:
                        logger.debug(
                            "⏱️ KIS rate-limit pause_until(%.1f)가 현재 시각(%.1f)보다 300초 이상 미래 — "
                            "이전 부팅 잔재로 간주하고 무시",
                            pause_until, now,
                        )
                        pause_until = 0.0
                    if pause_until > now:
                        pause_wait = pause_until - now
                        if op_name:
                            logger.debug("⏱️ KIS rate-limit 전역 대기: %s %.2f초 대기", op_name, pause_wait)
                        time.sleep(pause_wait)
                        now = time.monotonic()
                    apply_wait(last_seen)
                    cls._last_request_at = now
                    next_state = {"last_request_at": now}
                    if pause_until > now:
                        next_state["pause_until"] = pause_until
                    fp.seek(0)
                    fp.truncate()
                    json.dump(next_state, fp)
                    fp.flush()
                    fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                logger.debug("KIS throttle state 파일 사용 실패(%s) — 프로세스 내부 throttle만 적용", e)
                apply_wait(cls._last_request_at)
                cls._last_request_at = now

    @classmethod
    def _record_rate_limit_pause(cls, wait_seconds: float) -> None:
        """Rate limit 감지 시 다른 프로세스도 잠시 멈추도록 전역 pause를 기록한다."""
        wait_seconds = max(0.0, cls._to_float(wait_seconds))
        if wait_seconds <= 0:
            return
        now = time.monotonic()
        pause_until = now + wait_seconds
        state_path = Path(cls._throttle_state_path)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with state_path.open("a+", encoding="utf-8") as fp:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                fp.seek(0)
                try:
                    state = json.loads(fp.read() or "{}")
                except json.JSONDecodeError:
                    state = {}
                existing_pause = cls._to_float(state.get("pause_until"))
                state["pause_until"] = max(existing_pause, pause_until)
                state["last_request_at"] = max(cls._to_float(state.get("last_request_at")), cls._last_request_at, now)
                fp.seek(0)
                fp.truncate()
                json.dump(state, fp)
                fp.flush()
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.debug("KIS rate-limit pause 기록 실패: %s", e)

    # ── 인증 ──────────────────────────────────────────────
    def _get_access_token(self) -> str:
        """OAuth 접근토큰 발급 (만료 시 자동 갱신)."""
        now = time.time()
        if self._access_token and now < self._token_expires:
            return self._access_token
        if KISAPI._shared_access_token and now < KISAPI._shared_token_expires:
            self._access_token = KISAPI._shared_access_token
            self._token_expires = KISAPI._shared_token_expires
            return self._access_token

        with KISAPI._token_lock:
            now = time.time()
            if KISAPI._shared_access_token and now < KISAPI._shared_token_expires:
                self._access_token = KISAPI._shared_access_token
                self._token_expires = KISAPI._shared_token_expires
                return self._access_token

            url = f"{self.base_url}/oauth2/tokenP"
            headers = {"Content-Type": "application/json"}
            body = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            }

            # [Fix] 토큰 갱신에 재시도 로직 추가 — 403/500 시 바로 죽지 않고 재시도
            for attempt in range(1, 4):
                try:
                    resp = requests.post(url, headers=headers, json=body, timeout=10)
                    if resp.status_code >= 400:
                        body_text = resp.text[:500] if resp.text else ""
                        logger.warning(
                            "⚠️ 토큰 발급 실패(%d/3): HTTP %d | %s",
                            attempt, resp.status_code, body_text[:200],
                        )
                        if attempt < 3:
                            time.sleep(2 * attempt)
                            continue
                        resp.raise_for_status()
                    data = resp.json()
                    self._access_token = data["access_token"]
                    # 만료시간 23시간 후로 설정 (여유분 1시간)
                    self._token_expires = time.time() + (int(data.get("expires_in", 86400)) - 3600)
                    KISAPI._shared_access_token = self._access_token
                    KISAPI._shared_token_expires = self._token_expires
                    logger.info("✅ KIS 접근토큰 발급 성공 (모드: %s, 시도=%d)", self.mode, attempt)
                    return self._access_token
                except Exception as e:
                    logger.error("❌ KIS 접근토큰 발급 실패(%d/3): %s", attempt, e)
                    if attempt >= 3:
                        raise
                    time.sleep(2 * attempt)
            raise RuntimeError("토큰 발급 최종 실패")

    def _make_hashkey(self, body: dict) -> str:
        """KIS 주문 API 필수 hashkey 생성.
        
        HMAC-SHA256(app_secret, json_body) → base64 인코딩.
        KIS 서버는 hashkey 없는 POST 주문을 EGW00356으로 거부함.
        """
        body_str = json.dumps(body)
        hash_val = hmac.new(
            self.app_secret.encode(),
            body_str.encode(),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(hash_val).decode()

    def _headers(self, tr_id: str = "", body: dict = None) -> Dict[str, str]:
        """공통 요청 헤더. 주문 시 body를 넘기면 hashkey 자동 생성."""
        token = self._get_access_token()
        h = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "appKey": self.app_key,
            "appSecret": self.app_secret,
            "custtype": "P",  # 개인
        }
        if tr_id:
            h["tr_id"] = tr_id
            h["tr_cont"] = "N"
        # [CRITICAL] 주문 POST 요청 시 hashkey 필수 (없으면 EGW00356 에러)
        if body is not None:
            h["hashkey"] = self._make_hashkey(body)
        return h

    @classmethod
    def _resolve_us_exchange(cls, symbol: str) -> str:
        """미국 종목 거래소 코드 추정."""
        result = cls.US_EXCHANGE_BY_SYMBOL.get(str(symbol or "").upper(), None)
        if result is None:
            import logging
            logging.getLogger(__name__).warning(
                "⚠️ 거래소 맵에 없는 심볼: %s — NAS로 fallback (US_EXCHANGE_BY_SYMBOL 업데이트 필요)",
                symbol,
            )
            return "NAS"
        return result

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            if value in (None, "", " "):
                return 0.0
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            if value in (None, "", " "):
                return 0
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _parse_us_daily_rows(cls, resp_data: Dict[str, Any], period: int) -> List[Dict[str, Any]]:
        """해외 일봉 응답에서 봉 리스트 추출."""
        def is_daily_row(item: Any) -> bool:
            if not isinstance(item, dict):
                return False
            return (
                any(key in item for key in ("xymd", "date", "stck_bsop_date"))
                and any(key in item for key in ("clos", "close", "last"))
            )

        def find_row_container(node: Any) -> Optional[List[Dict[str, Any]]]:
            if isinstance(node, list):
                dict_items = [item for item in node if isinstance(item, dict)]
                if dict_items and all(is_daily_row(item) for item in dict_items):
                    return dict_items
                for item in node:
                    found = find_row_container(item)
                    if found:
                        return found
            elif isinstance(node, dict):
                for key in ("output2", "output1", "output", "items", "data"):
                    if key in node:
                        found = find_row_container(node.get(key))
                        if found:
                            return found
                for value in node.values():
                    found = find_row_container(value)
                    if found:
                        return found
            return None

        items = find_row_container(resp_data) or []

        parsed = []
        for item in items[:period]:
            date_value = item.get("xymd") or item.get("date") or item.get("stck_bsop_date") or ""
            close_value = item.get("clos", item.get("close", item.get("last", 0)))
            row = {
                "date": str(date_value),
                "open": cls._to_float(item.get("open")),
                "high": cls._to_float(item.get("high")),
                "low": cls._to_float(item.get("low")),
                "close": cls._to_float(close_value),
                "volume": cls._to_int(item.get("evol", item.get("volume", item.get("tvol", 0)))),
            }
            if row["date"] and row["close"] > 0:
                parsed.append(row)

        # KIS는 최신 → 과거 순 응답이 일반적이라 오래된 → 최신 순으로 정렬
        return list(reversed(parsed))

    @classmethod
    def _parse_us_balance_payload(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        output1 = data.get("output1", [])
        output2_raw = data.get("output2", [])
        if isinstance(output2_raw, list):
            output2 = output2_raw[0] if output2_raw else {}
        elif isinstance(output2_raw, dict):
            output2 = output2_raw
        else:
            output2 = {}

        stocks = []
        for item in output1:
            stocks.append({
                "symbol": item.get("ovrs_pdno", item.get("pdno", "")),
                "name": item.get("ovrs_item_name", item.get("prdt_name", "")),
                "quantity": cls._to_int(item.get("ovrs_cblc_qty", item.get("cblc_qty", 0))),
                "avg_price": cls._to_float(item.get("pchs_avg_pric", item.get("avg_pric", 0))),
                "current_price": cls._to_float(item.get("now_pric2", item.get("ovrs_now_pric1", 0))),
                "pnl": cls._to_float(item.get("evlu_pfls_amt", item.get("evlu_amt", 0))),
                "pnl_rate": cls._to_float(item.get("evlu_pfls_rt", item.get("evlu_rt", 0))),
                "exchange": item.get("ovrs_excg_cd", item.get("excg_dvsn_cd", "")),
            })

        total_candidates = ("tot_evlu_amt", "tot_asst_amt", "ovrs_tot_pfls", "frcr_evlu_amt2")
        available_candidates = ("frcr_dncl_one_amt", "ovrs_rlzt_pfls_amt", "frcr_buy_mgn_amt", "nxdy_frcr_drwg_psbl_amt")
        pnl_candidates = ("tot_evlu_pfls_amt", "ovrs_tot_pfls", "evlu_pfls_sum")

        def pick(candidates):
            for key in candidates:
                value = cls._to_float(output2.get(key))
                if value != 0:
                    return value, key
            return 0.0, candidates[0]

        total_usd, total_key = pick(total_candidates)
        available_usd, available_key = pick(available_candidates)
        total_pnl_usd, pnl_key = pick(pnl_candidates)
        msg_cd = str(data.get("msg_cd", ""))
        msg1 = str(data.get("msg1", ""))

        if msg_cd == "OPSQ0002":
            balance_status = "unsupported"
        elif total_usd > 0 or available_usd > 0 or stocks:
            balance_status = "ok"
        else:
            balance_status = "zero"

        return {
            "total_usd": total_usd,
            "available_usd": available_usd,
            "total_pnl_usd": total_pnl_usd,
            "stocks": stocks,
            "balance_status": balance_status,
            "status_code": msg_cd,
            "status_message": msg1,
            "_raw_output2": output2,
            "_parse_keys": {
                "total_usd": total_key,
                "available_usd": available_key,
                "total_pnl_usd": pnl_key,
            },
        }

    # ── 잔고 조회 ─────────────────────────────────────────

    def get_balance(self) -> Dict[str, Any]:
        """계좌 잔고 조회."""
        if self.mode == "real":
            tr_id = "TTTC8434R"  # 실전 잔고
        else:
            tr_id = "VTTC8434R"  # 모의 잔고

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",  # 종합
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        try:
            req = self._request_with_retry(
                "GET",
                url,
                op_name="잔고 조회",
                headers=self._headers(tr_id),
                params=params,
            )
            if not req.get("ok"):
                raise RuntimeError(f"{req.get('error_code')}: {req.get('error_message')}")
            data = req.get("data", {})
            output1 = data.get("output1", [])
            output2 = data.get("output2", [{}])[0]

            # 보유 종목 리스트
            stocks = []
            for item in output1:
                stocks.append({
                    "code": item.get("pdno", ""),           # 종목코드
                    "name": item.get("prdt_name", ""),       # 종목명
                    "quantity": int(item.get("hldg_qty", 0)),  # 보유수량
                    "avg_price": float(item.get("pchs_avg_pric", 0)),  # 매입단가
                    "current_price": float(item.get("prpr", 0)),  # 현재가
                    "pnl": float(item.get("evlu_pfls_amt", 0)),  # 평가손익
                    "pnl_rate": float(item.get("evlu_pfls_rt", 0)),  # 수익률 %
                })

            # 계좌 요약
            stock_eval = sum(s["current_price"] * s["quantity"] for s in stocks)
            summary = {
                "balance_status": "ok",
                "total_deposit": float(output2.get("dnca_tot_amt", 0)),    # 예수금총액
                "cash": float(output2.get("dnca_tot_amt", 0)),
                "total_eval": float(output2.get("tot_evlu_amt", 0)),       # 예수금 포함 총평가
                "total_assets": float(output2.get("tot_evlu_amt", 0)),
                "stock_eval": stock_eval,
                "total_pnl": float(output2.get("evlu_pfls_smtl_amt", 0)), # 총평가손익
                "stocks": stocks,
            }
            return summary
        except Exception as e:
            logger.error("❌ 잔고 조회 실패: %s", e)
            return {"balance_status": "failed", "total_deposit": 0, "cash": 0, "total_eval": 0, "total_assets": 0, "stock_eval": 0, "total_pnl": 0, "stocks": []}

    # ── 시세 조회 ─────────────────────────────────────────

    def get_stock_price(self, stock_code: str) -> Dict[str, Any]:
        """개별 종목 현재가 조회. KIS VTS 서버 500 에러 대비 재시도 강화 + 일봉 fallback."""
        tr_id = "FHKST01010100"
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 주식
            "FID_INPUT_ISCD": stock_code,
        }

        try:
            req = self._request_with_retry(
                "GET",
                url,
                op_name=f"국내 시세 조회({stock_code})",
                headers=self._headers(tr_id),
                params=params,
                retries=5,
                backoff=2.0,
            )
            if not req.get("ok"):
                # 현재가 조회 실패 → 일봉 전일종가로 fallback
                logger.warning("⚠️ 현재가 조회 실패(%s) — 일봉 전일종가로 대체 시도", stock_code)
                daily = self.get_stock_daily(stock_code, period=2)
                if daily:
                    fallback_price = daily[-1].get("close", 0)
                    if fallback_price > 0:
                        logger.info("↩️ 일봉 fallback 가격 사용: %s = %d", stock_code, fallback_price)
                        return {"code": stock_code, "current": fallback_price, "source": "daily_fallback"}
                return {
                    "code": stock_code,
                    "error_code": req.get("error_code", "PRICE_FETCH_FAILED"),
                    "error_message": req.get("error_message", "price fetch failed"),
                }
            data = req.get("data", {}).get("output", {})
            return {
                "code": stock_code,
                "name": data.get("hts_kor_isnm", ""),
                "current": int(data.get("stck_prpr", 0)),         # 현재가
                "open": int(data.get("stck_oprc", 0)),            # 시가
                "high": int(data.get("stck_hgpr", 0)),            # 고가
                "low": int(data.get("stck_lwpr", 0)),             # 저가
                "volume": int(data.get("acml_vol", 0)),           # 거래량
                "prev_close": int(data.get("stck_sdpr", 0)),      # 전일종가
                "change": int(data.get("prdy_vrss", 0)),          # 전일대비
                "change_rate": float(data.get("prdy_ctrt", 0)),   # 등락률
            }
        except Exception as e:
            logger.error("❌ 시세 조회 실패 (%s): %s", stock_code, e)
            return {}

    def get_stock_daily(self, stock_code: str, period: int = 30) -> List[Dict]:
        """종목 일봉 데이터 조회.
        KIS API는 최신→과거 순으로 반환하므로 reversed()로 오래된→최신 순 정렬.
        """
        tr_id = "FHKST03010100"
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": "",
            "FID_INPUT_DATE_2": "",
            "FID_PERIOD_DIV_CODE": "D",  # 일봉
            "FID_ORG_ADJ_PRC": "1",      # 수정주가
        }

        for attempt in range(5):
            try:
                req = self._request_with_retry(
                    "GET",
                    url,
                    op_name=f"국내 일봉 조회({stock_code})",
                    headers=self._headers(tr_id),
                    params=params,
                    retries=5,
                    backoff=1.5,
                )
                if not req.get("ok"):
                    logger.warning(
                        "⚠️ 일봉 조회 실패 (%s) [%d/5]: %s",
                        stock_code, attempt + 1,
                        req.get("error_message", ""),
                    )
                    if attempt < 4:
                        time.sleep(2 ** attempt)
                    continue
                resp_data = req.get("data", {})
                items = resp_data.get("output2", resp_data.get("output", []))
                if isinstance(items, list):
                    # KIS API는 최신→과거 순 반환 → reversed로 오래된→최신 순 변환
                    return list(reversed([
                        {
                            "date": item.get("stck_bsop_date", ""),
                            "open": int(item.get("stck_oprc", 0)),
                            "high": int(item.get("stck_hgpr", 0)),
                            "low": int(item.get("stck_lwpr", 0)),
                            "close": int(item.get("stck_clpr", 0)),
                            "volume": int(item.get("acml_vol", 0)),
                        }
                        for item in items[:period]
                    ]))
                return []
            except Exception as e:
                logger.warning("⚠️ 일봉 조회 예외 (%s) [%d/5]: %s", stock_code, attempt + 1, e)
                if attempt < 4:
                    time.sleep(2 ** attempt)
        logger.error("❌ 일봉 조회 최종 실패 (%s)", stock_code)
        return []

    # ── 호가단위 반올림 ─────────────────────────────────────
    @staticmethod
    def _round_kr_price_to_tick(price: int) -> int:
        """KRX 호가단위 규칙에 따라 가격을 유효한 값으로 반올림.

        KRX 호가단위:
        - < 2,000원: 1원
        - 2,000 ~ 4,999: 5원
        - 5,000 ~ 19,999: 10원
        - 20,000 ~ 49,999: 50원
        - 50,000 ~ 199,999: 100원
        - 200,000 ~ 499,999: 500원
        - ≥ 500,000: 1,000원

        2026-05-15: 모의투자 호가단위 오류(삼성전자 276,750) 대응 추가.
        """
        if price <= 0:
            return price
        tick = 1
        if price < 2000:
            tick = 1
        elif price < 5000:
            tick = 5
        elif price < 20000:
            tick = 10
        elif price < 50000:
            tick = 50
        elif price < 200000:
            tick = 100
        elif price < 500000:
            tick = 500
        else:
            tick = 1000
        return int(math.floor(price / tick + 0.5) * tick)

    # ── 주문 실행 ─────────────────────────────────────────

    def buy_stock(self, stock_code: str, quantity: int, price: int = 0) -> Dict[str, Any]:
        """매수 주문.

        Args:
            stock_code: 종목코드 (6자리)
            quantity: 수량
            price: 단가 (0이면 시장가, 양수면 자동 호가단위 반올림)
        """
        if self.mode == "real":
            tr_id = "TTTC0802U"  # 실전 매수
        else:
            tr_id = "VTTC0802U"  # 모의 매수

        # [Fix 2026-05-15] 호가단위 반올림 — 모의투자 호가단위 오류 방지
        if price > 0:
            price = self._round_kr_price_to_tick(price)

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        # [Fix] price=0이면 시장가("01"), 아니면 지정가("00")
        # 시장가 주문 시 ORD_UNPR must be "0" (KIS API 요구사항)
        order_type = "01" if price == 0 else "00"

        body = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "PDNO": stock_code,
            "ORD_DVSN": order_type,  # 00: 지정가, 01: 시장가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if price > 0 else "0",
        }

        # [Fix] 주문 요청 바디 상세 로깅 — 500 에러 원인 파악용
        logger.info(
            "📋 매수 주문 요청: %s qty=%d price=%d type=%s tr_id=%s account=%s-%s",
            stock_code, quantity, price, order_type, tr_id,
            self.account_no[:8], self.account_product,
        )

        try:
            req = self._request_with_retry(
                "POST",
                url,
                op_name=f"국내 매수 주문({stock_code})",
                headers=self._headers(tr_id, body=body),
                json=body,
            )
            if not req.get("ok"):
                return {
                    "success": False,
                    "error_code": req.get("error_code", "BUY_REQUEST_FAILED"),
                    "message": req.get("error_message", "요청 실패"),
                }
            data = req.get("data", {})
            if data.get("rt_cd") == "0":
                logger.info("✅ 매수 주문 성공: %s %d주 @%d", stock_code, quantity, price)
                return {"success": True, "order_id": data.get("output", {}).get("ODNO", ""), "data": data}
            else:
                logger.error("❌ 매수 주문 거절: %s", data.get("msg1", ""))
                return {
                    "success": False,
                    "error_code": data.get("msg_cd", "ORDER_REJECTED"),
                    "message": data.get("msg1", "주문 거절"),
                }
        except Exception as e:
            logger.error("❌ 매수 주문 실패: %s", e)
            return {"success": False, "error_code": "BUY_EXCEPTION", "message": str(e)}

    def sell_stock(self, stock_code: str, quantity: int, price: int = 0) -> Dict[str, Any]:
        """매도 주문."""
        if self.mode == "real":
            tr_id = "TTTC0801U"  # 실전 매도
        else:
            tr_id = "VTTC0801U"  # 모의 매도

        # [Fix 2026-05-15] 호가단위 반올림 — 모의투자 호가단위 오류 방지
        if price > 0:
            price = self._round_kr_price_to_tick(price)

        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        # [Claude Fix] 매도도 price=0이면 시장가("01"), 지정가는 "00"
        sell_order_type = "01" if price == 0 else "00"
        body = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "PDNO": stock_code,
            "ORD_DVSN": sell_order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if price > 0 else "0",
        }
        
        # [Fix] 매도 주문 요청 상세 로깅
        logger.info(
            "📋 매도 주문 요청: %s qty=%d price=%d type=%s tr_id=%s",
            stock_code, quantity, price, sell_order_type, tr_id,
        )

        try:
            req = self._request_with_retry(
                "POST",
                url,
                op_name=f"국내 매도 주문({stock_code})",
                headers=self._headers(tr_id, body=body),
                json=body,
            )
            if not req.get("ok"):
                return {
                    "success": False,
                    "error_code": req.get("error_code", "SELL_REQUEST_FAILED"),
                    "message": req.get("error_message", "요청 실패"),
                }
            data = req.get("data", {})
            if data.get("rt_cd") == "0":
                logger.info("✅ 매도 주문 성공: %s %d주 @%d", stock_code, quantity, price)
                return {"success": True, "order_id": data.get("output", {}).get("ODNO", ""), "data": data}
            else:
                logger.error("❌ 매도 주문 거절: %s", data.get("msg1", ""))
                return {
                    "success": False,
                    "error_code": data.get("msg_cd", "ORDER_REJECTED"),
                    "message": data.get("msg1", "주문 거절"),
                }
        except Exception as e:
            logger.error("❌ 매도 주문 실패: %s", e)
            return {"success": False, "error_code": "SELL_EXCEPTION", "message": str(e)}

    # ── 해외주식 (미국) ─────────────────────────────────────

    def get_us_stock_price(self, symbol: str) -> Dict[str, Any]:
        """미국 종목 현재가 조회.
        
        Args:
            symbol: 티커 심볼 (예: NVDA, AAPL)
        """
        tr_id = "HHDFS00000300"
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        params = {
            "AUTH": "",
            "EXCD": self._resolve_us_exchange(symbol),
            "SYMB": symbol,
        }

        try:
            req = self._request_with_retry(
                "GET",
                url,
                op_name=f"미국 시세 조회({symbol})",
                headers=self._headers(tr_id),
                params=params,
            )
            if not req.get("ok"):
                return {
                    "symbol": symbol,
                    "error_code": req.get("error_code", "US_PRICE_FETCH_FAILED"),
                    "error_message": req.get("error_message", "price fetch failed"),
                    "exchange": params["EXCD"],
                }
            data = req.get("data", {}).get("output", {})
            return {
                "symbol": symbol,
                "name": data.get("name", symbol),
                "current": float(data.get("last", 0)),
                "open": float(data.get("open", 0)),
                "high": float(data.get("high", 0)),
                "low": float(data.get("low", 0)),
                "volume": int(data.get("evol", 0)),
                "prev_close": float(data.get("base", 0)),
                "change": float(data.get("diff", 0)),
                "change_rate": float(data.get("rate", 0)),
                "exchange": params["EXCD"],
            }
        except Exception as e:
            logger.error("❌ 미국 시세 조회 실패 (%s): %s", symbol, e)
            return {}

    # 모의투자(VTS)에서 일부 미국 종목이 정상 응답(rt_cd=0)이지만
    # output2가 빈 배열로 반환됨. API 호출 낭비 + 헬스 이슈 노이즈 방지.
    _US_EMPTY_RESPONSE_SYMBOLS = frozenset({"TDOC", "ABB"})

    def get_us_stock_daily(self, symbol: str, period: int = 30) -> List[Dict]:
        """미국 종목 일봉 데이터 조회."""
        if symbol in self._US_EMPTY_RESPONSE_SYMBOLS:
            logger.debug(
                "🔇 미국 일봉 스킵 (%s): KIS VTS에서 지속적으로 빈 응답 반환",
                symbol,
            )
            return []
        tr_id = "HHDFS76240000"
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/dailyprice"
        excd = self._resolve_us_exchange(symbol)
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": symbol,
            "GUBN": "0",
            "BYMD": "",
            "MODP": "1",
        }

        for attempt in range(3):
            try:
                req = self._request_with_retry(
                    "GET",
                    url,
                    op_name=f"미국 일봉 조회({symbol})",
                    headers=self._headers(tr_id),
                    params=params,
                )
                if not req.get("ok"):
                    kis_code = req.get("kis_code", "")
                    if kis_code in {"EGW00201", "EGW00131", "EGW00132"}:
                        logger.warning(
                            "⚠️ 미국 일봉 조회 rate limit (%s/%s) [%d/3]: %s",
                            symbol, excd, attempt + 1, req.get("error_message", ""),
                        )
                    else:
                        logger.warning(
                            "⚠️ 미국 일봉 조회 실패 (%s/%s) [%d/3]: %s",
                            symbol, excd, attempt + 1, req.get("error_message", ""),
                        )
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                    continue
                resp_data = req.get("data", {})
                parsed_rows = self._parse_us_daily_rows(resp_data, period)
                if parsed_rows:
                    return parsed_rows

                data_keys = sorted(resp_data.keys()) if isinstance(resp_data, dict) else []
                output1 = resp_data.get("output1")
                output1_keys = sorted(output1.keys())[:20] if isinstance(output1, dict) else []
                output1_preview = {}
                if isinstance(output1, dict):
                    for key in output1_keys[:5]:
                        value = output1.get(key)
                        if isinstance(value, (str, int, float)):
                            output1_preview[key] = value
                        elif isinstance(value, list):
                            output1_preview[key] = f"list[{len(value)}]"
                        elif isinstance(value, dict):
                            output1_preview[key] = f"dict[{len(value)}]"
                        else:
                            output1_preview[key] = type(value).__name__
                logger.warning(
                    "⚠️ 미국 일봉 빈 응답 (%s/%s): rt_cd=%s msg_cd=%s msg1=%s keys=%s output1=%s output1_keys=%s output1_preview=%s output2=%s output=%s — 정상 응답의 데이터 없음으로 간주하고 재시도 생략",
                    symbol,
                    excd,
                    resp_data.get("rt_cd", ""),
                    resp_data.get("msg_cd", ""),
                    str(resp_data.get("msg1", ""))[:160],
                    data_keys,
                    type(resp_data.get("output1")).__name__,
                    output1_keys,
                    output1_preview,
                    type(resp_data.get("output2")).__name__,
                    type(resp_data.get("output")).__name__,
                )
                return []
            except Exception as e:
                logger.warning("⚠️ 미국 일봉 조회 예외 (%s) [%d/3]: %s", symbol, attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt)
        logger.error("❌ 미국 일봉 조회 최종 실패 (%s/%s)", symbol, excd)
        return []

    def buy_us_stock(self, symbol: str, quantity: int, price: float = 0) -> Dict[str, Any]:
        """미국 주식 매수 주문."""
        # [CRITICAL FIX] 해외주식 TR_ID = TTTS (S 포함!)
        # 실전: TTTS1002U, 모의: VTTS1002U (hashkey 필수!)
        if self.mode == "real":
            tr_id = "TTTS1002U"
        else:
            tr_id = "VTTS1002U"

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        excd = self._resolve_us_exchange(symbol)
        # [Fix] 미국 매수: 지정가("00") / 시장가 구분
        # NASDAQ 시장가="81", NYSE 시장가="82" — 지정가는 항상 "00"
        if price > 0:
            order_type = "00"  # 지정가
        else:
            order_type = "81" if excd == "NAS" else "82"  # 거래소별 시장가

        # [CRITICAL FIX] ORD_DVSN 필드가 누락되어 있었음 — KIS API 필수 필드
        body = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": excd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,  # ← 필수: 00=지정가, 81=NASDAQ시장가, 82=NYSE시장가
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": str(price) if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
        }

        # [Fix] rate limit 회피: 주문 전 1초 대기 (EGW00201 방지)
        time.sleep(1.0)

        logger.info(
            "📋 미국 매수 주문: %s qty=%d price=%.2f type=%s excd=%s tr_id=%s",
            symbol, quantity, price, order_type, excd, tr_id,
        )

        try:
            req = self._request_with_retry(
                "POST",
                url,
                op_name=f"미국 매수 주문({symbol})",
                headers=self._headers(tr_id, body=body),
                json=body,
                retries=5,
                backoff=2.0,
            )
            if not req.get("ok"):
                return {
                    "success": False,
                    "error_code": req.get("error_code", "US_BUY_REQUEST_FAILED"),
                    "message": req.get("error_message", "요청 실패"),
                }
            data = req.get("data", {})
            if data.get("rt_cd") == "0":
                logger.info("✅ 미국 매수 성공: %s %d주 @%.2f", symbol, quantity, price)
                return {"success": True, "order_id": data.get("output", {}).get("ODNO", ""), "data": data}
            else:
                logger.error("❌ 미국 매수 거절: %s", data.get("msg1", ""))
                return {
                    "success": False,
                    "error_code": data.get("msg_cd", "ORDER_REJECTED"),
                    "message": data.get("msg1", "주문 거절"),
                }
        except Exception as e:
            logger.error("❌ 미국 매수 실패: %s", e)
            return {"success": False, "error_code": "US_BUY_EXCEPTION", "message": str(e)}

    def sell_us_stock(self, symbol: str, quantity: int, price: float = 0) -> Dict[str, Any]:
        """미국 주식 매도 주문."""
        # [CRITICAL FIX] 해외주식 TR_ID = TTTS (S 포함!)
        # 실전: TTTS1001U, 모의: VTTS1001U (hashkey 필수!)
        if self.mode == "real":
            tr_id = "TTTS1001U"
        else:
            tr_id = "VTTS1001U"

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        excd = self._resolve_us_exchange(symbol)
        # [CRITICAL FIX] ORD_DVSN 추가 — 매도도 동일하게 필요
        if price > 0:
            order_type = "00"  # 지정가
        else:
            order_type = "81" if excd == "NAS" else "82"  # 시장가

        body = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": excd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,  # ← 필수: 00=지정가, 81/82=시장가
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": str(price) if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
        }

        # [Fix] rate limit 회피
        time.sleep(1.0)

        logger.info(
            "📋 미국 매도 주문: %s qty=%d price=%.2f type=%s excd=%s tr_id=%s",
            symbol, quantity, price, order_type, excd, tr_id,
        )

        try:
            req = self._request_with_retry(
                "POST",
                url,
                op_name=f"미국 매도 주문({symbol})",
                headers=self._headers(tr_id, body=body),
                json=body,
                retries=5,
                backoff=2.0,
            )
            if not req.get("ok"):
                return {
                    "success": False,
                    "error_code": req.get("error_code", "US_SELL_REQUEST_FAILED"),
                    "message": req.get("error_message", "요청 실패"),
                }
            data = req.get("data", {})
            if data.get("rt_cd") == "0":
                logger.info("✅ 미국 매도 성공: %s %d주 @%.2f", symbol, quantity, price)
                return {"success": True, "order_id": data.get("output", {}).get("ODNO", ""), "data": data}
            else:
                logger.error("❌ 미국 매도 거절: %s", data.get("msg1", ""))
                return {
                    "success": False,
                    "error_code": data.get("msg_cd", "ORDER_REJECTED"),
                    "message": data.get("msg1", "주문 거절"),
                }
        except Exception as e:
            logger.error("❌ 미국 매도 실패: %s", e)
            return {"success": False, "error_code": "US_SELL_EXCEPTION", "message": str(e)}

    def get_us_balance(self) -> Dict[str, Any]:
        """미국 주식 잔고 조회."""
        # 해외주식 잔고: 실전=TTTS3019R, 모의=VTTS3019R
        if self.mode == "real":
            tr_id = "TTTS3019R"
        else:
            tr_id = "VTTS3019R"

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": "NAS",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        # [Fix] rate limit 회피
        time.sleep(0.5)

        try:
            req = self._request_with_retry(
                "GET",
                url,
                op_name="미국 잔고 조회",
                headers=self._headers(tr_id),
                params=params,
                retries=5,
                backoff=2.0,
            )
            if not req.get("ok"):
                logger.warning("⚠️ 미국 잔고 조회 실패: %s", req.get("error_message", ""))
                return {"total_usd": 0, "available_usd": 0, "total_pnl_usd": 0, "stocks": []}
            data = req.get("data", {})
            parsed = self._parse_us_balance_payload(data)
            if parsed.get("balance_status") != "ok":
                logger.warning(
                    "⚠️ 미국 잔고 상태 이상: status=%s parse_keys=%s output2_keys=%s stocks=%d msg_cd=%s msg1=%s",
                    parsed.get("balance_status", "unknown"),
                    parsed.get("_parse_keys", {}),
                    sorted(parsed.get("_raw_output2", {}).keys()),
                    len(parsed.get("stocks", [])),
                    data.get("msg_cd", ""),
                    str(data.get("msg1", ""))[:160],
                )
            return {
                "total_usd": parsed["total_usd"],
                "available_usd": parsed["available_usd"],
                "total_pnl_usd": parsed["total_pnl_usd"],
                "stocks": parsed["stocks"],
                "balance_status": parsed.get("balance_status", "failed"),
                "status_code": parsed.get("status_code", ""),
                "status_message": parsed.get("status_message", ""),
            }
        except Exception as e:
            logger.error("❌ 미국 잔고 조회 실패: %s", e)
            return {
                "total_usd": 0,
                "available_usd": 0,
                "total_pnl_usd": 0,
                "stocks": [],
                "balance_status": "failed",
                "status_code": "EXCEPTION",
                "status_message": str(e),
            }

    def get_exchange_rate(self) -> float:
        """USD/KRW 실시간 환율 조회."""
        tr_id = "HHDFS00000000"
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/exchange-rate"
        params = {
            "AUTH": "",
            "EXCD": "NAS",
            "SYMB": "USD",
        }

        try:
            resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("output", {})
            rate = float(data.get("rate", 1350))
            return rate
        except Exception as e:
            logger.warning("⚠️ 환율 조회 실패, 기본값 사용: %s", e)
            return 1350.0

    # ── 유틸리티 ──────────────────────────────────────────

    def test_connection(self) -> bool:
        """API 연결 테스트."""
        try:
            token = self._get_access_token()
            if token:
                logger.info("✅ KIS API 연결 성공 (모드: %s)", self.mode)
                return True
            return False
        except Exception:
            return False

    def test_order_capability(self) -> Dict[str, Any]:
        """주문 API 가용성 테스트.
        
        VTS 서버에서 EGW00356 에러가 발생하는지 확인.
        실제 주문은 하지 않고, 서버 응답만 확인합니다.
        """
        result = {
            "can_connect": False,
            "can_order_domestic": False,
            "can_order_overseas": False,
            "mode": self.mode,
            "base_url": self.base_url,
            "errors": [],
        }
        
        # 1. 토큰 발급 테스트
        try:
            token = self._get_access_token()
            result["can_connect"] = True
        except Exception as e:
            result["errors"].append(f"토큰 발급 실패: {e}")
            return result
        
        # 2. 국내 주문 테스트 (실제 주문 안 함 — 서버 응답만 확인)
        if self.mode == "real":
            domestic_tr_id = "TTTC0802U"
        else:
            domestic_tr_id = "VTTC0802U"
        
        domestic_url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        domestic_body = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "PDNO": "000000",  # 존재하지 않는 코드 — 실제 주문 안 됨
            "ORD_DVSN": "01",
            "ORD_QTY": "0",    # 0수량 — 실제 주문 안 됨
            "ORD_UNPR": "0",
        }
        
        try:
            req = self._request_with_retry(
                "POST",
                domestic_url,
                op_name="국내 주문 테스트",
                headers=self._headers(domestic_tr_id, body=domestic_body),
                json=domestic_body,
                retries=1,
            )
            kis_code = req.get("kis_code", "")
            if kis_code in ("EGW00356", "EGW02006", "EGW02004"):
                result["errors"].append(f"국내 주문 불가: {kis_code} ({req.get('error_message', '')[:100]})")
                result["can_order_domestic"] = False
            else:
                # 어떤 에러든 EGW00356이 아니면 서버 라우팅은 정상
                result["can_order_domestic"] = True
        except Exception as e:
            result["errors"].append(f"국내 주문 테스트 예외: {e}")
        
        # 3. 해외 주문 테스트
        if self.mode == "real":
            overseas_tr_id = "TTTS1002U"
        else:
            overseas_tr_id = "VTTS1002U"
        
        overseas_url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        overseas_body = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": "NAS",
            "PDNO": "TEST",
            "ORD_DVSN": "00",
            "ORD_QTY": "0",
            "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0",
        }
        
        try:
            req = self._request_with_retry(
                "POST",
                overseas_url,
                op_name="해외 주문 테스트",
                headers=self._headers(overseas_tr_id, body=overseas_body),
                json=overseas_body,
                retries=1,
            )
            kis_code = req.get("kis_code", "")
            if kis_code in ("EGW00356", "EGW02006", "EGW02004"):
                result["errors"].append(f"해외 주문 불가: {kis_code} ({req.get('error_message', '')[:100]})")
                result["can_order_overseas"] = False
            else:
                result["can_order_overseas"] = True
        except Exception as e:
            result["errors"].append(f"해외 주문 테스트 예외: {e}")
        
        # 종합 판정
        result["can_trade"] = result["can_order_domestic"] or result["can_order_overseas"]
        
        if not result["can_trade"]:
            logger.error(
                "🚨 주문 API 불가! KIS %s 모드에서 주문이 차단되었습니다. "
                "EGW00356 = 서버 라우팅 실패. "
                "해결: KIS 고객센터 문의(1588-6611) 또는 실전 모드 전환(./switch_mode.sh real)",
                self.mode.upper(),
            )
        else:
            logger.info("✅ 주문 API 가용성 확인 (국내=%s, 해외=%s)", 
                       result["can_order_domestic"], result["can_order_overseas"])
        
        return result
