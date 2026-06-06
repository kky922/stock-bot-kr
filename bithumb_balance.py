#!/usr/bin/env python3
"""Bithumb 계좌 잔고 조회 (현재가 기준) — JWT 인증 v2 API (HS256)"""

import os
import sys
import json
import time
import uuid
import hmac
import hashlib
import base64
import requests

# Load .env
env_path = os.path.expanduser("~/stock_bot/.env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

ACCESS_KEY = os.environ.get("BITHUMB_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("BITHUMB_SECRET_KEY", "")

# Use secret key as-is (base64 string, NOT decoded)
# Node.js jsonwebtoken library uses the raw string, so we do too
SECRET_KEY_RAW = SECRET_KEY

if not ACCESS_KEY or not SECRET_KEY:
    print("❌ BITHUMB keys not found in .env")
    sys.exit(1)

def base64url_encode(data):
    """Base64 URL-safe encoding without padding"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

def create_jwt(payload, secret):
    """Create JWT with HS256 algorithm"""
    # Header
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = base64url_encode(json.dumps(header, separators=(",", ":")))
    
    # Payload
    payload_b64 = base64url_encode(json.dumps(payload, separators=(",", ":")))
    
    # Signing input
    signing_input = f"{header_b64}.{payload_b64}"
    
    # Sign with HMAC-SHA256 (HS256)
    signature = base64url_encode(
        hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256
        ).digest()
    )
    
    return f"{signing_input}.{signature}"

def get_balances():
    """Bithumb v2 API — 전체 계좌 조회"""
    url = "https://api.bithumb.com/v1/accounts"
    
    # JWT payload (no query params)
    payload = {
        "access_key": ACCESS_KEY,
        "nonce": str(uuid.uuid4()),
        "timestamp": int(time.time() * 1000),
    }
    
    jwt_token = create_jwt(payload, SECRET_KEY_RAW)
    
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    
    resp = requests.get(url, headers=headers, timeout=10)
    
    if resp.status_code != 200:
        print(f"❌ API error {resp.status_code}: {resp.text}")
        print(f"\nDebug - first 80 chars of token: {jwt_token[:80]}...")
        return None
    
    return resp.json()

def get_current_prices(symbols):
    """Bithumb public ticker API"""
    prices = {}
    for sym in symbols:
        try:
            resp = requests.get(
                f"https://api.bithumb.com/public/ticker/{sym}_KRW",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "0000":
                    prices[sym] = float(data["data"].get("closing_price", "0"))
        except Exception:
            pass
        time.sleep(0.1)
    return prices

def main():
    print("💰 Bithumb 계좌 조회\n")
    
    balances = get_balances()
    if balances is None:
        return
    
    if not balances:
        print("보유 코인이 없습니다.")
        return
    
    non_zero = [b for b in balances if float(b.get("balance", "0")) > 0]
    
    if not non_zero:
        print("보유 코인이 없습니다.")
        return
    
    symbols = [b["currency"] for b in non_zero if b["currency"] != "KRW"]
    current_prices = get_current_prices(symbols)
    
    total_buy = 0
    total_current = 0
    
    print(f"{'코인':<10} {'보유량':<15} {'매입단가':<15} {'현재가':<15} {'매입액':<15} {'평가액':<15} {'수익률':<10}")
    print("-" * 100)
    
    results = []
    for b in non_zero:
        currency = b["currency"]
        balance = float(b["balance"])
        avg_price = float(b.get("avg_buy_price", "0") or "0")
        
        if currency == "KRW":
            current_price = 1.0
            buy_amount = balance
            current_amount = balance
        else:
            buy_amount = balance * avg_price
            current_price = current_prices.get(currency, 0)
            current_amount = balance * current_price
        
        total_buy += buy_amount
        total_current += current_amount
        
        if avg_price > 0 and current_price > 0 and currency != "KRW":
            pnl_pct = ((current_price - avg_price) / avg_price) * 100
            pnl_str = f"{pnl_pct:+.2f}%"
        else:
            pnl_str = "-"
        
        results.append({
            "currency": currency,
            "balance": balance,
            "avg_price": avg_price,
            "current_price": current_price,
            "buy_amount": buy_amount,
            "current_amount": current_amount,
        })
        
        if currency == "KRW":
            print(f"{currency:<10} {balance:<15,.4f} {'-':<15} {'-':<15} {'-':<15} {balance:<15,.0f} {'-':<10}")
        else:
            print(f"{currency:<10} {balance:<15.4f} {avg_price:<15,.0f} {current_price:<15,.0f} {buy_amount:<15,.0f} {current_amount:<15,.0f} {pnl_str:<10}")
    
    print("-" * 100)
    
    non_krw_results = [r for r in results if r["currency"] != "KRW"]
    non_krw_results.sort(key=lambda x: x["current_amount"], reverse=True)
    
    krw_balance = next((r for r in results if r["currency"] == "KRW"), None)
    
    print(f"\n📊 요약")
    print(f"{'코인':<10} {'평가액':<15} {'비중':<10}")
    print("-" * 35)
    for r in non_krw_results:
        pct = (r["current_amount"] / total_current * 100) if total_current > 0 else 0
        print(f"{r['currency']:<10} {r['current_amount']:<15,.0f} {pct:<10.1f}%")
    
    if krw_balance:
        pct = (krw_balance["current_amount"] / total_current * 100) if total_current > 0 else 0
        print(f"{'KRW':<10} {krw_balance['current_amount']:<15,.0f} {pct:<10.1f}%")
    
    print("-" * 35)
    print(f"{'합계':<10} {total_current:<15,.0f} 100.0%")
    
    print(f"\n{'='*50}")
    print(f"💰 평가액 합계: {total_current:,.0f}원")
    if total_buy > 0 and any(r["currency"] != "KRW" for r in results):
        non_krw_buy = sum(r["buy_amount"] for r in non_krw_results)
        non_krw_current = sum(r["current_amount"] for r in non_krw_results)
        pnl = non_krw_current - non_krw_buy
        pnl_pct = (pnl / non_krw_buy * 100) if non_krw_buy > 0 else 0
        print(f"📈 코인 손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)")

if __name__ == "__main__":
    main()
