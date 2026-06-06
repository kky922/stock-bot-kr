"""
주말 백테스트 대량 실행 + 분석 리포트 자동 생성.
여러 종목 × 여러 전략 조합으로 백테스트 후 문제점과 개선안을 기록합니다.
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).parent
sys.path.insert(0, str(ROOT_DIR))

import config
from backtester import Backtester

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT_DIR / "logs" / "weekend_backtest.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

def load_stocks_from_theme_db() -> Dict[str, str]:
    """테마DB에서 모든 종목을 로드 (중복 제거)."""
    db_file = ROOT_DIR / "data" / "theme_db.json"
    try:
        with open(db_file, encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        logger.warning("⚠️ theme_db.json 로드 실패")
        return {}

    stocks = {}
    for theme_name, theme_data in db.get("themes", {}).items():
        for stock in theme_data.get("stocks_krx", []):
            code = stock.get("code", "")
            name = stock.get("name", "")
            if code and code not in stocks:
                stocks[code] = f"{name}({theme_name})"
    logger.info(f"📊 테마DB에서 {len(stocks)}개 종목 로드")
    return stocks

STRATEGIES = ["ma_cross", "rsi", "gap", "volatility", "all"]


def run_batch(stocks: Dict[str, str]):
    """전체 종목 × 전략 백테스트 실행."""
    bt = Backtester(initial_capital=10_000_000)
    results: List[Dict] = []
    
    total = len(stocks) * len(STRATEGIES)
    current = 0

    for code, name in stocks.items():
        for strategy in STRATEGIES:
            current += 1
            logger.info(f"\n{'='*50}")
            logger.info(f"[{current}/{total}] {name}({code}) - {strategy}")
            logger.info(f"{'='*50}")
            
            try:
                result = bt.run(stock_code=code, strategy=strategy)
                result["stock_name"] = name
                results.append(result)
                
                if result.get("success"):
                    logger.info(
                        f"✅ 수익률: {result['total_return']:+.2f}% | "
                        f"승률: {result['win_rate']:.1f}% | "
                        f"MDD: {result['max_drawdown']:.2f}% | "
                        f"거래: {result['total_trades']}회"
                    )
                else:
                    logger.warning(f"❌ 실패: {result.get('error', 'unknown')}")
                    
            except Exception as e:
                logger.error(f"❌ 예외: {e}")
                results.append({
                    "success": False,
                    "stock_code": code,
                    "stock_name": name,
                    "strategy": strategy,
                    "error": str(e),
                })
            
            # API 호출 제한 방지 (1분당 1회 토큰 발급)
            time.sleep(65)
    
    return results


def analyze_results(results: List[Dict], stocks: Dict[str, str]) -> Dict[str, Any]:
    """백테스트 결과 종합 분석."""
    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    
    # 전략별 평균 성과
    strategy_stats = {}
    for s in STRATEGIES:
        strat_results = [r for r in successful if r.get("strategy") == s]
        if not strat_results:
            continue
        
        returns = [r["total_return"] for r in strat_results]
        win_rates = [r["win_rate"] for r in strat_results]
        mdds = [r["max_drawdown"] for r in strat_results]
        trades = [r["total_trades"] for r in strat_results]
        pf = [r["profit_factor"] for r in strat_results if r["profit_factor"] > 0]
        
        profitable = sum(1 for ret in returns if ret > 0)
        
        strategy_stats[s] = {
            "count": len(strat_results),
            "avg_return": sum(returns) / len(returns),
            "max_return": max(returns),
            "min_return": min(returns),
            "avg_win_rate": sum(win_rates) / len(win_rates),
            "avg_mdd": sum(mdds) / len(mdds),
            "avg_trades": sum(trades) / len(trades),
            "avg_pf": sum(pf) / len(pf) if pf else 0,
            "profitable_count": profitable,
            "profitable_rate": profitable / len(strat_results) * 100,
        }
    
    # 종목별 최고 전략
    stock_best = {}
    for code, label in stocks.items():
        stock_results = [r for r in successful if r.get("stock_code") == code]
        if stock_results:
            best = max(stock_results, key=lambda x: x["total_return"])
            name = label.split("(")[0] if "(" in label else label
            stock_best[code] = {
                "name": name,
                "best_strategy": best["strategy"],
                "return": best["total_return"],
                "win_rate": best["win_rate"],
                "mdd": best["max_drawdown"],
            }
    
    # 수익 낸 종목 vs 손실 종목
    profit_stocks = [(code, data) for code, data in stock_best.items() if data["return"] > 0]
    loss_stocks = [(code, data) for code, data in stock_best.items() if data["return"] <= 0]
    
    # ── 문제점 분석 ──
    issues = []
    
    # 1. 승률이 너무 낮은 경우
    low_win = [r for r in successful if r["win_rate"] < 40 and r["total_trades"] > 2]
    if low_win:
        issues.append({
            "issue": "승률 40% 미만 전략 다수",
            "count": len(low_win),
            "detail": "매도 타이밍이 늦거나, 거짓 신호가 많음",
            "affected": [f"{r.get('stock_name','')}({r['stock_code']})-{r['strategy']}" for r in low_win[:5]],
        })
    
    # 2. 거래가 너무 적은 경우
    low_trades = [r for r in successful if r["total_trades"] <= 1]
    if low_trades:
        issues.append({
            "issue": "거래 횟수 1회 이하 (신호 부족)",
            "count": len(low_trades),
            "detail": "전략이 보수적이어서 진입 기회가 거의 없음",
            "affected": [f"{r.get('stock_name','')}({r['stock_code']})-{r['strategy']}" for r in low_trades[:5]],
        })
    
    # 3. MDD가 너무 큰 경우
    high_mdd = [r for r in successful if r["max_drawdown"] > 15]
    if high_mdd:
        issues.append({
            "issue": "MDD 15% 초과 (리스크 과다)",
            "count": len(high_mdd),
            "detail": "손절가 없이 물타기하거나, 한 번에 큰 손실",
            "affected": [f"{r.get('stock_name','')}({r['stock_code']})-{r['strategy']} (MDD:{r['max_drawdown']:.1f}%)" for r in high_mdd[:5]],
        })
    
    # 4. 수익률이 음수인 경우가 많은지
    neg_return = [r for r in successful if r["total_return"] < 0]
    if len(neg_return) > len(successful) * 0.5:
        issues.append({
            "issue": "수익률 음수 비율 50% 초과",
            "count": len(neg_return),
            "detail": "전략 자체의 유효성 재검토 필요",
        })
    
    return {
        "total_runs": len(results),
        "successful": len(successful),
        "failed": len(failed),
        "strategy_stats": strategy_stats,
        "stock_best": stock_best,
        "profit_stocks_count": len(profit_stocks),
        "loss_stocks_count": len(loss_stocks),
        "issues": issues,
        "failed_details": [{"code": r.get("stock_code"), "name": r.get("stock_name"), "strategy": r.get("strategy"), "error": r.get("error")} for r in failed],
    }


def generate_report(analysis: Dict, results: List[Dict]) -> str:
    """마크다운 리포트 생성."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    report = f"""# 📊 주말 백테스트 종합 리포트
생성일: {now}

---

## 📈 전체 요약

| 항목 | 값 |
|------|-----|
| 총 실행 | {analysis['total_runs']}건 |
| 성공 | {analysis['successful']}건 |
| 실패 | {analysis['failed']}건 |
| 수익 종목 | {analysis['profit_stocks_count']}개 |
| 손실 종목 | {analysis['loss_stocks_count']}개 |

---

## 🎯 전략별 성과 비교

| 전략 | 평균수익률 | 최고 | 최저 | 평균승률 | 평균MDD | 평균거래 | 수익비율 |
|------|----------|------|------|---------|---------|---------|---------|
"""
    for s, stats in analysis["strategy_stats"].items():
        report += f"| {s} | {stats['avg_return']:+.2f}% | {stats['max_return']:+.2f}% | {stats['min_return']:+.2f}% | {stats['avg_win_rate']:.1f}% | {stats['avg_mdd']:.2f}% | {stats['avg_trades']:.1f}회 | {stats['profitable_rate']:.0f}% |\n"
    
    report += f"""
---

## 🏆 종목별 최고 전략

| 종목 | 최고전략 | 수익률 | 승률 | MDD |
|------|---------|--------|------|-----|
"""
    for code, data in sorted(analysis["stock_best"].items(), key=lambda x: x[1]["return"], reverse=True):
        report += f"| {data['name']}({code}) | {data['best_strategy']} | {data['return']:+.2f}% | {data['win_rate']:.1f}% | {data['mdd']:.2f}% |\n"
    
    # 문제점
    report += "\n---\n\n## ⚠️ 발견된 문제점\n\n"
    if analysis["issues"]:
        for i, issue in enumerate(analysis["issues"], 1):
            report += f"### {i}. {issue['issue']}\n"
            report += f"- **건수**: {issue['count']}건\n"
            report += f"- **원인**: {issue['detail']}\n"
            if issue.get("affected"):
                report += f"- **영향받은 항목**: {', '.join(issue['affected'][:5])}\n"
            report += "\n"
    else:
        report += "특이사항 없음\n"
    
    # 개선 제안
    report += """---

## 💡 개선 제안

### 1. 손절/익절 자동 적용 (우선순위: 🔴 최우선)
**문제**: 현재 백테스트는 시그널 기반 매도만 사용 → MDD 과다
**개선**:
- 매수 후 `-5%` 손절 자동 실행
- 매수 후 `+10%` 익절 자동 실행
- `trailing stop` (추적 손절) 적용 → 수익 지키기

### 2. 포지션 사이징 개선 (우선순위: 🟡 높음)
**문제**: 항상 95% 자금 투입 → 리스크 과대
**개선**:
- 신호 강도에 따라 30%/50%/70% 분할 매수
- Kelly 기준 적용: `f = (p*b - q) / b`
- 종목당 최대 20% 자금 제한

### 3. 거짓 신호 필터링 (우선순위: 🟡 높음)
**문제**: 승률이 낮은 전략 존재 → 거짓 신호 다수
**개선**:
- 거래량 필터: 평균 거래량의 150% 이상일 때만 매수
- 트렌드 필터: 60일 이평선 위에 있을 때만 매수
- 볼린저 밴드 활용: 밴드 하단에서 반등 시 매수

### 4. 다중 시간프레임 분석 (우선순위: 🟢 보통)
**문제**: 일봉만 사용 → 단기 노이즈에 취약
**개선**:
- 주봉 트렌드 + 일봉 시그널 조합
- 60분봉 진입 타이밍 세분화
- 월봉 장기 트렌드 필터

### 5. 전략 조합 최적화 (우선순위: 🟢 보통)
**문제**: 단일 전략은 시장 상황에 따라 성과 편차 큼
**개선**:
- 시장 국면 판별 (상승/하락/횡보)
- 국면별 최적 전략 자동 선택
- 앙상블: 상위 2개 전략에서 동시 신호 시 매수

### 6. 백테스트 엔진 개선 (우선숸위: 🟢 보통)
**문제**: 100일 데이터만 사용 → 장기 검증 부족
**개선**:
- KIS API 분봉/주봉/월봉 추가 조회
- 슬리피지(매수/매도 가격 차이) 반영
- 수수료(매매수수료 0.015%) 반영
- 세금(매도세 0.18%) 반영

### 7. 데이터 확보 (우선숸위: 🔴 최우선)
**문제**: KIS API 100일 데이터 제한
**개선**:
- 일봉 데이터 캐싱 (매일 누적 저장)
- 야후 파이낸스 / 네이버 금융 크롤링으로 과거 데이터 확보
- 3년~5년 치 과거 데이터 확보 후 재백테스트

---

## 📅 월요일 실행 계획

### 오전 (모의투자)
1. **09:00** 봇 시작 → 뉴스 스캔 시작
2. **09:00~15:30** 실시간 모니터링
3. 매수 시그널 → 모의투자로 자동 실행
4. 손절/익절 자동 적용
5. 텔레그램으로 실시간 알림

### 오후 (결과 분석)
1. **15:30** 장 마감 후 일일 리포트 생성
2. 백테스트 결과 vs 실제 모의투자 결과 비교
3. 전략 미세 조정

### 라이브 적용 조건
- ✅ 모의투자 3일 이상 정상 동작
- ✅ 일평균 수익 > 0 (또는 MDD < 5%)
- ✅ 승률 > 45%
- ✅ 손절/익절 정상 작동 확인
- ✅ 텔레그램 알림 정상 확인

---
"""
    
    return report


def main():
    logger.info("=" * 60)
    logger.info("🚀 주말 백테스트 대량 실행 시작")
    STOCKS = load_stocks_from_theme_db()
    total = len(STOCKS) * len(STRATEGIES)
    logger.info(f"📊 대상: {len(STOCKS)}개 종목 × {len(STRATEGIES)}개 전략 = {total}조합")
    logger.info("=" * 60)
    
    # 1) 백테스트 실행
    results = run_batch(STOCKS)
    
    # 2) 결과 분석
    logger.info("\n📊 결과 분석 중...")
    analysis = analyze_results(results, STOCKS)
    
    # 3) 리포트 생성
    report = generate_report(analysis, results)
    report_file = ROOT_DIR / "BACKTEST_REPORT.md"
    report_file.write_text(report, encoding="utf-8")
    logger.info(f"📝 리포트 저장: {report_file}")
    
    # 4) JSON 결과 저장
    results_file = ROOT_DIR / "logs" / "backtest_results.json"
    save_data = {
        "timestamp": datetime.now().isoformat(),
        "analysis": analysis,
        "results": [{k: v for k, v in r.items() if k != "equity_curve"} for r in results],
    }
    results_file.write_text(json.dumps(save_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"💾 결과 저장: {results_file}")
    
    # 5) 요약 출력
    logger.info("\n" + "=" * 60)
    logger.info("📊 백테스트 요약")
    logger.info("=" * 60)
    for s, stats in analysis["strategy_stats"].items():
        logger.info(f"  {s:12s}: 평균 {stats['avg_return']:+.2f}% | 승률 {stats['avg_win_rate']:.1f}% | MDD {stats['avg_mdd']:.2f}%")
    logger.info(f"\n🏆 최고 전략: {max(analysis['strategy_stats'].items(), key=lambda x: x[1]['avg_return'])[0]}")
    logger.info(f"📝 전체 리포트: {report_file}")
    logger.info("✅ 완료!")


if __name__ == "__main__":
    main()