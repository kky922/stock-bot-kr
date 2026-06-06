#!/usr/bin/env python3
"""KIS VTS 주문 테스트 - 국내 vs 해외 비교"""

# pytest가 이 스크립트를 테스트로 수집하지 않게 막는다.
__test__ = False

import json, hmac, hashlib, base64, requests, sys, os
from dotenv import load_dotenv
load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")
ACCT_NO = os.getenv("KIS_ACCOUNT_NO", "")
ACCT_PRDT = os.getenv("KIS_ACCOUNT_PRODUCT", "01")
BASE_URL = "https://openapivts.koreainvestment.com:29443"

print(f"BASE_URL: {BASE_URL}")
print(f"ACCT: {ACCT_NO[:4]}****")

# 토큰 발급
resp = requests.post(f"{BASE_URL}/oauth2/tokenP", headers={"Content-Type": "application/json"},
    json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}, timeout=10)
token = resp.json().get("access_token", "")
if not token:
    print(f"❌ 토큰 발급 실패: {resp.text[:200]}")
    sys.exit(1)
print(f"✅ 토큰 OK")

def make_hashkey(body_dict):
    body_str = json.dumps(body_dict)
    h = hmac.new(APP_SECRET.encode(), body_str.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()

def test_order(name, url, tr_id, body):
    hashkey = make_hashkey(body)
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "custtype": "P",
        "tr_id": tr_id,
        "hashkey": hashkey,
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        rj = resp.json()
        rt = rj.get("rt_cd", "?")
        msg = rj.get("msg1", "")
        code = rj.get("msg_cd", "")
        ok = "✅ 성공" if rt == "0" else f"❌ {code}"
        print(f"  {name} ({tr_id}): {ok} | {msg[:80]}")
        return rt == "0"
    except Exception as e:
        print(f"  {name}: ❌ 예외 {e}")
        return False

print("\n=== 1. 국내 주식 주문 테스트 ===")
domestic_url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
domestic_body = {
    "CANO": ACCT_NO[:8],
    "ACNT_PRDT_CD": ACCT_PRDT,
    "PDNO": "005930",
    "ORD_DVSN": "01",
    "ORD_QTY": "1",
    "ORD_UNPR": "0",
}
test_order("모의 국내 매수", domestic_url, "VTTC0802U", domestic_body)

print("\n=== 2. 해외 주식 주문 테스트 ===")
overseas_url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
overseas_body = {
    "CANO": ACCT_NO[:8],
    "ACNT_PRDT_CD": ACCT_PRDT,
    "OVRS_EXCG_CD": "NAS",
    "PDNO": "NVDA",
    "ORD_DVSN": "00",
    "ORD_QTY": "1",
    "OVRS_ORD_UNPR": "100.00",
    "ORD_SVR_DVSN_CD": "0",
}
test_order("모의(VTTS) 해외 매수", overseas_url, "VTTS1002U", overseas_body)
test_order("실전(TTTS) 해외 매수", overseas_url, "TTTS1002U", overseas_body)

print("\n=== 3. 해외주식 다른 포트 테스트 (9443) ===")
alt_url = "https://openapivts.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/order"
test_order("VTS:9443 + TTTS", alt_url, "TTTS1002U", overseas_body)
test_order("VTS:9443 + VTTS", alt_url, "VTTS1002U", overseas_body)

print("\n=== 4. 해외주식 다른 포트 테스트 (443/기본) ===")
alt_url2 = "https://openapivts.koreainvestment.com/uapi/overseas-stock/v1/trading/order"
test_order("VTS:443 + TTTS", alt_url2, "TTTS1002U", overseas_body)
test_order("VTS:443 + VTTS", alt_url2, "VTTS1002U", overseas_body)