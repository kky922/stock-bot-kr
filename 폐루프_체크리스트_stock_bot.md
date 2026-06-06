# stock_bot 폐루프 체크리스트

> 최종 업데이트: 2026-06-02

## Phase 0 — 공통 기반

| 항목 | 상태 | 비고 |
|------|------|------|
| Bridge target 분류 — stock-bot 독립 타겟 지원 | ✅ 완료 | `classify_targets` list 반환 |
| `infra/context_from.py` — ContextSnapshot + check_tier2_trigger | ✅ 완료 | `stock_bot/infra/context_from.py` |
| Two-Tier Tier1 (매 사이클 해시 체크) | ✅ 완료 | `run_system()` while 루프 상단 |
| Two-Tier Tier2 (브리지 신호 시 텔레그램 알림) | ✅ 완료 | `run_system()` `_run_tier2` 블록 |
| `_last_bridge_hash` 추적 변수 | ✅ 완료 | `run_system()` 함수 내 |

## Phase 1 — stock_bot 핵심 구현

| 항목 | 상태 | 비고 |
|------|------|------|
| 7-Agent 파이프라인 (뉴스→테마→후보→기술→진입→리스크→실행) | ✅ 기존 | `run_agents.py:run_pipeline_once()` |
| Bridge PROPOSE가 stock-bot 타겟일 때만 Tier2 동작 | ✅ 완료 | `ctx.targets_stock()` 체크 |
| `all_slots.json` synced_at 기반 슬롯 관리 | ✅ 기존 | `DataStore.save_slot()` |
| 수동 포지션 감지 및 등록 | ✅ 기존 | `_sync_positions_from_account()` |
| Pending Entry — 장 마감 후 BUY 대기 | ✅ 기존 | `_process_pending_entries()` |
| 슬롯 교체 (약한 포지션 → 강한 후보) | ✅ 기존 | `_select_replacement_slot()` |
| 포지션 모니터 스레드 (60초 주기) | ✅ 기존 | `_monitor_loop()` |
| 런타임 정책 (`block_new_entries`, `conservative_mode`) | ✅ 기존 | `infra/runtime_policy.py` |

## Phase 2 — 피드백 루프

| 항목 | 상태 | 비고 |
|------|------|------|
| 거래 기록 (`trades.json`) 저장 | ✅ 기존 | `DataStore.append_trade()` |
| `closed_loop_feedback.py`에서 stock_bot PnL 집계 | ✅ 완료 | `STOCK_TRADES_DB` |
| 추천 히스토리 기록 (`recommendation_history`) | ✅ 기존 | `store.append_recommendation()` |

## 독립 실행 테스트

```bash
# context_from Tier1/Tier2 테스트
cd ~/stock_bot && python3 -c "
from infra.context_from import check_tier2_trigger
run, ctx = check_tier2_trigger('stock-bot')
print('Tier2:', run, '| regime:', ctx.regime, '| targets:', ctx.targets)
"

# 파이프라인 1회 실행 (--once 플래그)
python3 run_agents.py --once

# 연결 테스트
python3 run_agents.py --test

# 상태 확인
python3 run_agents.py --status
```

## context_from chain end-to-end 테스트

```bash
# 1회차: bridge 실행
python3 ~/.hermes/scripts/info_strategy_closed_loop_bridge.py

# 2회차: stock-bot 컨텍스트 체크 (no_change 확인)
python3 -c "
from infra.context_from import check_tier2_trigger, load_last_hash
print('last_hash:', load_last_hash())
run, ctx = check_tier2_trigger('stock-bot')
print('trigger:', run, ctx)
"

# 3회차: bridge_state.json의 hash 값을 임시로 바꿔 Tier2 트리거 강제 확인
```

## no_change 상황 agent 호출 방지 확인

- `check_tier2_trigger()` 반환값 `(False, ctx)` 시 Tier2 로직 스킵 확인
- 로그에서 `"Tier2 skipped"` 메시지 확인
- 브리지가 갱신될 때만 `"Tier2 triggered"` 로그 발생 확인
