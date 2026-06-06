"""
주식 백테스트 엔진.
KIS 일봉 데이터로 과거 전략 시뮬레이션을 수행합니다.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).parent
sys.path.insert(0, str(ROOT_DIR))

import config
from kis_api import KISAPI
from technical import TechnicalAnalyzer

logger = logging.getLogger(__name__)


class Backtester:
    """과거 데이터 기반 전략 백테스트."""

    def __init__(self, initial_capital: float = 10_000_000):
        self.initial_capital = initial_capital
        self.kis = KISAPI()
        self.ta = TechnicalAnalyzer()

    def run(
        self,
        stock_code: str,
        strategy: str = "all",
        start_date: str = "",
        end_date: str = "",
        stop_loss: float = -5.0,
        take_profit: float = 10.0,
    ) -> Dict[str, Any]:
        """백테스트 실행.

        Args:
            stock_code: 종목코드 (6자리)
            strategy: "all" | "ma_cross" | "rsi" | "gap" | "volatility"
            start_date: 시작일 (YYYYMMDD, 빈값이면 전체)
            end_date: 종료일
        """
        # 1) 일봉 데이터 로드
        print(f"📊 {stock_code} 일봉 데이터 조회 중...")
        daily = self.kis.get_stock_daily(stock_code, period=100)  # 최대 100일
        if not daily or len(daily) < 30:
            return {"success": False, "error": f"데이터 부족 ({len(daily)}건, 30건 이상 필요)"}

        # 날짜 역순 → 시간순 정렬 (KIS는 최신순으로 반환)
        daily.reverse()

        # 날짜 필터
        if start_date:
            daily = [d for d in daily if d.get("date", "") >= start_date]
        if end_date:
            daily = [d for d in daily if d.get("date", "") <= end_date]

        print(f"📅 데이터: {daily[0]['date']} ~ {daily[-1]['date']} ({len(daily)}일)")

        # 2) 백테스트 시뮬레이션
        capital = self.initial_capital
        position = 0  # 보유 수량
        entry_price = 0.0
        trades: List[Dict] = []
        equity_curve: List[Dict] = []

        for i in range(20, len(daily)):  # 20일 이평선 때문에 20일부터 시작
            window = daily[:i + 1]
            today = daily[i]
            price = today["close"]
            date = today["date"]

            # ── 손절 / 익절 체크 (최우선) ──
            if position > 0 and entry_price > 0:
                pnl_rate = (price - entry_price) / entry_price * 100
                if pnl_rate <= stop_loss:
                    capital += position * price
                    trades.append({
                        "date": date, "action": "sell(손절)", "price": price,
                        "quantity": position, "pnl": (price - entry_price) * position,
                        "pnl_rate": pnl_rate, "capital": capital,
                    })
                    position = 0
                    entry_price = 0.0
                    equity = capital
                    equity_curve.append({"date": date, "equity": equity, "price": price})
                    continue
                elif pnl_rate >= take_profit:
                    capital += position * price
                    trades.append({
                        "date": date, "action": "sell(익절)", "price": price,
                        "quantity": position, "pnl": (price - entry_price) * position,
                        "pnl_rate": pnl_rate, "capital": capital,
                    })
                    position = 0
                    entry_price = 0.0
                    equity = capital
                    equity_curve.append({"date": date, "equity": equity, "price": price})
                    continue

            # 신호 생성
            signal = self._get_signal(window, strategy)

            # 매수
            if signal == "buy" and position == 0:
                qty = int(capital * 0.95 / price)  # 95% 자금 사용
                if qty > 0:
                    entry_price = price
                    position = qty
                    capital -= qty * price
                    trades.append({
                        "date": date, "action": "buy", "price": price,
                        "quantity": qty, "capital": capital + qty * price,
                    })

            # 매도
            elif signal == "sell" and position > 0:
                capital += position * price
                pnl = (price - entry_price) * position
                pnl_rate = (price - entry_price) / entry_price * 100
                trades.append({
                    "date": date, "action": "sell", "price": price,
                    "quantity": position, "pnl": pnl, "pnl_rate": pnl_rate,
                    "capital": capital,
                })
                position = 0
                entry_price = 0.0

            # 자산 곡선 기록
            equity = capital + position * price
            equity_curve.append({"date": date, "equity": equity, "price": price})

        # 미청산 포지션 정산
        if position > 0:
            last_price = daily[-1]["close"]
            capital += position * last_price
            trades.append({
                "date": daily[-1]["date"], "action": "sell(정산)", "price": last_price,
                "quantity": position, "capital": capital,
            })
            position = 0

        # 3) 결과 분석
        result = self._analyze(trades, equity_curve)
        result["stock_code"] = stock_code
        result["strategy"] = strategy
        result["data_period"] = f"{daily[0]['date']} ~ {daily[-1]['date']}"
        result["data_days"] = len(daily)
        result["initial_capital"] = self.initial_capital
        result["trades"] = trades
        result["equity_curve"] = equity_curve

        return result

    def _get_signal(self, window: List[Dict], strategy: str) -> str:
        """전략별 신호 생성."""
        if strategy == "ma_cross":
            r = self.ta.ma_cross_signal(window)
            return r.get("signal", "hold")
        elif strategy == "rsi":
            r = self.ta.rsi_signal(window)
            return r.get("signal", "hold")
        elif strategy == "gap":
            r = self.ta.gap_signal(window)
            return r.get("signal", "hold")
        elif strategy == "volatility":
            r = self.ta.volatility_breakout_signal(window)
            return r.get("signal", "hold")
        else:  # all
            r = self.ta.analyze_all(window)
            return r.get("final_signal", "hold")

    def _analyze(self, trades: List[Dict], equity_curve: List[Dict]) -> Dict[str, Any]:
        """거래 결과 분석."""
        buy_trades = [t for t in trades if t["action"] == "buy"]
        sell_trades = [t for t in trades if t["action"].startswith("sell")]

        # 완결 거래 (매수-매도 쌍)
        completed = []
        buy_idx = 0
        for t in trades:
            if t["action"] == "buy" and buy_idx < len(sell_trades):
                sell = sell_trades[buy_idx] if buy_idx < len(sell_trades) else None
                if sell:
                    completed.append({
                        "buy_date": t["date"],
                        "sell_date": sell["date"],
                        "buy_price": t["price"],
                        "sell_price": sell["price"],
                        "pnl": sell.get("pnl", (sell["price"] - t["price"]) * t["quantity"]),
                        "pnl_rate": sell.get("pnl_rate", (sell["price"] - t["price"]) / t["price"] * 100),
                    })
                    buy_idx += 1

        # 승률
        wins = [c for c in completed if c["pnl"] > 0]
        losses = [c for c in completed if c["pnl"] <= 0]
        win_rate = (len(wins) / len(completed) * 100) if completed else 0

        # 최종 자본
        final_capital = equity_curve[-1]["equity"] if equity_curve else self.initial_capital
        total_return = (final_capital - self.initial_capital) / self.initial_capital * 100

        # 최대 낙폭 (MDD)
        peak = self.initial_capital
        max_dd = 0
        for eq in equity_curve:
            if eq["equity"] > peak:
                peak = eq["equity"]
            dd = (peak - eq["equity"]) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 평균 수익/손실
        avg_win = sum(c["pnl"] for c in wins) / len(wins) if wins else 0
        avg_loss = sum(c["pnl"] for c in losses) / len(losses) if losses else 0

        return {
            "success": True,
            "total_trades": len(buy_trades),
            "completed_trades": len(completed),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "final_capital": final_capital,
            "total_return": total_return,
            "total_pnl": final_capital - self.initial_capital,
            "max_drawdown": max_dd,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": abs(sum(c["pnl"] for c in wins) / sum(c["pnl"] for c in losses)) if losses and sum(c["pnl"] for c in losses) != 0 else 0,
            "completed_details": completed,
        }

    def print_report(self, result: Dict[str, Any]):
        """백테스트 결과 리포트 출력."""
        if not result.get("success"):
            print(f"❌ 백테스트 실패: {result.get('error', 'unknown')}")
            return

        print("\n" + "=" * 60)
        print(f"📊 백테스트 결과: {result['stock_code']}")
        print(f"📅 기간: {result['data_period']} ({result['data_days']}일)")
        print(f"🎯 전략: {result['strategy']}")
        print("=" * 60)
        print(f"💰 초기 자본:   {self.initial_capital:>12,.0f}원")
        print(f"💰 최종 자본:   {result['final_capital']:>12,.0f}원")
        print(f"📈 총 수익:     {result['total_pnl']:>12,.0f}원 ({result['total_return']:+.2f}%)")
        print(f"📉 최대 낙폭:   {result['max_drawdown']:>12.2f}%")
        print(f"🔄 총 거래:     {result['total_trades']:>12d}회")
        print(f"✅ 완결 거래:   {result['completed_trades']:>12d}회")
        print(f"🏆 승률:        {result['win_rate']:>12.1f}%")
        print(f"   승: {result['win_count']}회 | 패: {result['loss_count']}회")
        print(f"📊 평균 수익:   {result['avg_win']:>12,.0f}원")
        print(f"📊 평균 손실:   {result['avg_loss']:>12,.0f}원")
        print(f"📊 손익비(PF):  {result['profit_factor']:>12.2f}")
        print("=" * 60)

        # 거래 내역
        if result.get("completed_details"):
            print("\n📋 거래 내역:")
            print(f"{'매수일':>12} {'매도일':>12} {'매수가':>10} {'매도가':>10} {'수익률':>8}")
            print("-" * 60)
            for c in result["completed_details"][:20]:
                print(f"{c['buy_date']:>12} {c['sell_date']:>12} "
                      f"{c['buy_price']:>10,.0f} {c['sell_price']:>10,.0f} "
                      f"{c['pnl_rate']:>+7.2f}%")

        print()


def main():
    """CLI 백테스트 실행."""
    import argparse
    parser = argparse.ArgumentParser(description="주식 백테스트")
    parser.add_argument("stock_code", help="종목코드 (예: 005930)")
    parser.add_argument("--strategy", "-s", default="all",
                        choices=["all", "ma_cross", "rsi", "gap", "volatility"],
                        help="전략 선택")
    parser.add_argument("--capital", "-c", type=float, default=10_000_000,
                        help="초기 자본 (기본: 1000만)")
    parser.add_argument("--start", default="", help="시작일 (YYYYMMDD)")
    parser.add_argument("--end", default="", help="종료일 (YYYYMMDD)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    bt = Backtester(initial_capital=args.capital)
    result = bt.run(
        stock_code=args.stock_code,
        strategy=args.strategy,
        start_date=args.start or "",
        end_date=args.end or "",
    )
    bt.print_report(result)

    # 결과 JSON 저장
    out_file = config.LOGS_DIR / f"backtest_{args.stock_code}_{args.strategy}.json"
    # equity_curve는 파일에서 제외 (너무 큼)
    save_result = {k: v for k, v in result.items() if k != "equity_curve"}
    out_file.write_text(json.dumps(save_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"💾 결과 저장: {out_file}")


if __name__ == "__main__":
    main()