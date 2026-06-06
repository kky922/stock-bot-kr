# Stock Bot 알려진 이슈 & 해결 기록

## 11. 봇 시동 시 `_throttle_request()`가 `pause_until` 변칙값으로 2시간 정지 (CRITICAL — fixed 2026-05-15)

**증상:** 봇 재시작 후 `test_order_capability()` 직후 로그가 1시간 넘게 멈춤. 
프로세스는 살아있고 CPU 0%. `agent_system.log` 가장 마지막 줄: `"🔍 주문 API 가용성 확인 중..."`.
실제로 주문 가용성 확인 단계가 아니라 `_throttle_request()` 내부에서 무한에 가까운 sleep 중.
Health collector: `"agent_system.log가 53분 이상 갱신되지 않음"` + 봇 기동 무한 지연.

**근원(원인):** 
- `_record_rate_limit_pause()`가 이전 부팅 시 `pause_until`을 throttle 파일에 저장 (`kis_request_throttle.json`)
- 시스템 재부팅 후 `time.monotonic()` 클럭이 리셋됨 (7446s → 0s)
- `_throttle_request()` 235-239행: `if pause_until > now:` 조건이 여과 없이 True → 
  `time.sleep(pause_until - now)` = `time.sleep(7451)` ≈ **2시간 sleep**
- `last_request_at`에는 보호 로직(`> now + 60` → return)이 있었지만, `pause_until`에는 없었음

**수정:** `kis_api.py` `_throttleRequest()`: `pause_until > now + 300`이면 이전 부팅 잔재로 간주하고 `pause_until = 0.0`으로 리셋.

**진단:**
```bash
# throttle 파일 확인 (pause_until이 현재 monotonic보다 300초 이상 큰가?)
cat ~/stock_bot/data/agents/kis_request_throttle.json

# 현재 monotonic 클럭
python3 -c "import time; print('monotonic:', time.monotonic())"

# 전체 테스트
cd ~/stock_bot && python3 -m pytest tests/test_kis_api_parsing.py::TestKISAPIParsing::test_global_throttle_ignores_stale_pause_until_from_previous_boot -v
```

**회피:** throttle 파일 수동 리셋:
```bash
echo '{"last_request_at": 0.0, "pause_until": 0.0}' > ~/stock_bot/data/agents/kis_request_throttle.json
```

**Pitfall:** Docker 컨테이너 재시작, macOS 재부팅, VM 스냅샷 복원 등 `time.monotonic()`이 리셋되는 모든 환경에서 재현 가능. `pause_until`은 rate-limit 대기 시간을 5~15초 기록하므로 정상 값은 절대 `now + 300`을 초과하지 않음.
