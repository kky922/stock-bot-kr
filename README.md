# Stock Bot — 한국투자증권 KIS 자동매매봇

뉴스 기반 이슈 스윙 트레이딩 봇. 한국(KIS) + 미국 주식 동시 운영.  
국내는 낮, 미국은 밤에 자동 스캔 → AI 분석 → 메가테마/리더주 선별 → 5-Layer 진입 검증 → 자동 매매.

---

## 아키텍처

```
뉴스 수집 (Google RSS)
    ↓
메가테마 분석 (AI GLM-5.1)
    ↓
테마별 후보 추출 + 역할 분류 (MarketScout)
    ↓
리더/모멘텀 선별 순위 (TechnicalAnalyst)
    ↓
5-Layer 진입 검증 (EntryAnalyzer)
    ↓
리스크 체크 (RiskManagerAgent)
    ↓
매매 실행 (TradeExecutorAgent)
    ↓
실시간 모니터 (MonitorAgent — 별도 스레드, 60초 주기)
```

### 5-Layer 진입 검증

| Layer | 조건 | 설명 |
|-------|------|------|
| 1 | 신호 점수 ≥ 7 | 뉴스/AI 신호 품질 |
| 2 | 기술 추세 buy/hold | MA 크로스, RSI, MACD |
| 3 | 거래량 ≥ 1.2x | 20일 평균 대비 거래량 비율 |
| 4 | 당일 상승 ≤ 2% | 추격 매수 방지 |
| 5 | 저항선 근처 아님 | 지지/저항 위치 확인 |

- 5/5 통과 → `STRONG_BUY`
- 4/5 통과 → `BUY`
- 3/5 통과 → `WEAK_BUY`
- 2/5 이하 → `REJECT`

---

## 현재 운영 원칙

- **한국장(KR)**: `KIS_MODE=virtual` 기준 모의투자로 안정성 검증
- **미국장(US)**: 기본 정책은 `US_READINESS_MODE=true` 이지만, 로컬 설정으로 `virtual` 자동매매 전환 가능
- **포지션 원장**: `data/agents/all_slots.json` 단일 기준
- **중복 주문 방지**: 종목별 `inflight lock` + `cooldown`
- **시장별 헬스 상태 분리**: KR/US의 주문 가능 여부, 마지막 정상 잔고, API 장애 상태를 각각 기록

### KR / US 운용 모드

| 시장 | 기본 모드 | 목적 |
|------|-----------|------|
| KR | `virtual` | 신호/청산/상태 일관성 검증 |
| US | `readiness` | 라이브 전환 전 주문 파라미터/계좌 상태 검증 |

### US Readiness Mode

`US_READINESS_MODE=true` 일 때는:

- 미국장 시그널은 정상 생성됩니다.
- 주문 직전 검증과 추천 이력 기록은 수행됩니다.
- 실제 미국 주문은 실행하지 않습니다.
- 라이브 전환 전 `real` 모드, 주문 가능 계좌 상태, 주문 한도를 먼저 점검하기 위한 안전장치입니다.

### US Virtual Autotrade

로컬 환경에서 아래처럼 설정하면 미국장도 `virtual` 주문으로 자동매매됩니다.

```env
KIS_MODE=virtual
US_STOCK_ENABLED=true
US_READINESS_MODE=false
US_REQUIRE_REAL_MODE=false
```

- 미국장 후보 탐색, 매수, 슬롯 저장, 모니터 기반 청산까지 KR과 같은 흐름으로 동작합니다.
- `US_MICRO_LIVE_MAX_NOTIONAL`는 `real` 모드에서만 적용됩니다.
- repo 기본 정책은 readiness 유지이며, 실제 전환은 로컬 `.env` 기준입니다.

---

## 실행 방법

```bash
# 연결 테스트
python run_agents.py --test

# 현재 상태 확인
python run_agents.py --status

# 파이프라인 1회 실행 (디버깅용)
python run_agents.py --once

# 정상 실행 (스케줄 루프)
python run_agents.py
```

### 장시간 자동 스케줄

- **한국 장중** (09:00~15:30): 5분 주기 스캔
- **미국 장중** (22:30~06:00 KST, 썸머타임): 5분 주기 스캔
- **장외**: 30분 주기 스캔
- **모니터 스레드**: 항상 1분 주기 독립 실행 (손절/익절 감시)

---

## 설정 (.env)

```env
# 한국투자증권 API
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=...
KIS_MODE=virtual          # virtual | real

# AI
ZAI_API_KEY=...
ZAI_MODEL=glm-5.1

# 텔레그램 알림
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# 거래 설정
STOP_LOSS_PERCENT=5.0     # 손절 %
TAKE_PROFIT_PERCENT=10.0  # 익절 %
MAX_DAILY_LOSS_PERCENT=5.0
KR_BUDGET=5000000         # 국내 자금 (원)
US_BUDGET=5000            # 미국 자금 (USD)

# 진입 필터
AI_SCORE_MIN=7            # AI 최소 점수
ENTRY_MAX_CHASE_PCT=2.0   # 당일 최대 허용 상승 %
ENTRY_MIN_VOL_RATIO=1.2   # 최소 거래량 배율

# 런타임 보호장치
ORDER_COOLDOWN_SECONDS=900
ORDER_INFLIGHT_TTL_SECONDS=120
API_DEGRADED_RETRY_LIMIT=3

# 미국장 전환 준비
US_READINESS_MODE=true
US_REQUIRE_REAL_MODE=true
US_MICRO_LIVE_MAX_NOTIONAL=300
US_MIN_CASH_BUFFER=100
```

### 새로 추가된 주요 설정

| 변수 | 의미 |
|------|------|
| `ORDER_COOLDOWN_SECONDS` | 동일 종목 청산 후 재진입 대기 시간 |
| `ORDER_INFLIGHT_TTL_SECONDS` | 주문 진행중 락 유지 시간 |
| `API_DEGRADED_RETRY_LIMIT` | API 연속 장애 후 해당 시장을 observe-only 로 전환하는 기준 |
| `US_READINESS_MODE` | `true`면 미국장 실주문 비활성화, `false`면 실제 주문 경로 사용 |
| `US_REQUIRE_REAL_MODE` | `true`면 미국장 주문은 `real` 모드에서만 허용, `false`면 `virtual`도 허용 |
| `US_MICRO_LIVE_MAX_NOTIONAL` | 미국장 `real` 모드 초기 최대 주문 금액 |
| `US_MIN_CASH_BUFFER` | 미국장 주문 후 남겨둘 현금 버퍼 |

---

## 파일 구조

```
stock_bot/
├── run_agents.py          # 메인 엔트리포인트 (파이프라인 루프 + 모니터 스레드)
├── config.py              # 설정 (환경변수 로드)
├── kis_api.py             # 한국투자증권 KIS API 클라이언트
├── technical.py           # 기술적 지표 계산 (MA, RSI, MACD, ATR, 볼린저)
├── news_scanner.py        # 뉴스 RSS 수집 + 점수화
├── dashboard.py           # Flask 웹 대시보드 (포트 8501)
├── start_bot.sh           # 시작/중지/재시작/상태 스크립트
├── watchdog_stock.sh      # 프로세스 감시 자동 재시작
├── switch_mode.sh         # 모의↔실전 모드 전환
├── backtester.py          # 백테스트 엔진
├── weekend_backtest.py    # 주말 자동 백테스트
├── agents/
│   ├── orchestrator.py    # 파이프라인 조율 (뉴스→테마→리더주→기술→진입)
│   ├── entry_analyzer.py  # 5-Layer 진입 타점 검증
│   ├── monitor_agent.py   # 포지션 손절/익절/트레일링 감시
│   ├── trade_executor.py  # 주문 실행 (매수/매도)
│   ├── risk_manager_agent.py  # 자금/리스크 검증
│   ├── market_scout.py    # 종목 후보 추출 + 역할 분류 + 반복 추천 패널티
│   ├── technical_analyst.py  # 리더/모멘텀 기술 순위
│   └── theme_accumulator.py  # 메가테마 감지 (32개 테마)
├── core/
│   ├── data_store.py      # 슬롯 원장 / 추천이력 / 시장 헬스 상태 저장
│   └── news_archive.py    # 뉴스 중복 제거 + 아카이브 + 출처/신선도 요약
├── data/
│   ├── theme_db.json      # 테마-종목 매핑 DB (26개 테마, KR+US 종목)
│   └── risk_state.json    # 리스크 상태 (연속손실, 일일손실 등)
├── templates/
│   └── dashboard.html     # 웹 대시보드 HTML 템플릿
├── pids/                  # PID 파일 (봇, 대시보드)
└── logs/                  # 로그 파일
```

---

## 포지션 관리 / 런타임 보호

- **단일 포지션 원장**: 모든 포지션은 `data/agents/all_slots.json` 에 저장
- **자동 등록**: 봇이 매수한 종목은 `slot_id={MARKET}_{CODE}` 로 슬롯 저장
- **수동 등록**: 앱에서 직접 매수 시 파이프라인 시작마다 계좌 동기화 → 슬롯 등록 + 텔레그램 알림
- **손절**: 기본 5% (ATR 기반 동적 조정)
- **익절**: 기본 10% (ATR 기반 동적 조정)
- **트레일링 스탑**: 수익 조건 충족 시 활성화, ATR × 1.5 추적
- **중복 주문 방지**: 주문 중에는 `inflight lock`, 청산 후에는 `cooldown`
- **시장별 API 상태 분리**: KR/US의 `api_degraded_mode`, `last_good_balance`, `balance_sync_status` 를 각각 유지

### 저장되는 주요 런타임 필드

- 포지션:
  - `slot_id`, `market`, `code`, `name`, `entry_price`, `quantity`
  - `stop_loss_price`, `take_profit_price`, `highest_price`
  - `theme`, `strategy_id`, `order_state`, `last_order_attempt_at`
- 시장 상태:
  - `last_good_balance`, `last_good_deposit`
  - `balance_sync_status`
  - `consecutive_api_failures`
  - `api_degraded_mode`
- 추천/선별:
  - `recent_alert_count`
  - `recent_entry_count`
  - `recent_stopout_count`
  - `selection_score`
  - `relative_strength_score`
  - `volume_score`
  - `breakout_score`

---

## 종목 선택 로직

### 이전 방식

- 뉴스/테마 점수가 높은 테마의 대표 종목군을 후보로 올림
- 기술 점수보다 테마 점수가 크게 작용
- 테마 강도가 쉽게 `10.0` 으로 포화
- 결과적으로 `삼성전자 / SK하이닉스 / 한미반도체` 같은 대표주 반복 추천 발생

### 현재 방식

1. **메가테마 감지**
- 기사 수만이 아니라 출처 다양성, 키워드 밀도, 최근성까지 반영
- 동일 키워드 반복 기사에는 패널티 부여
- 테마 강도는 쉽게 최대치에 붙지 않도록 완만하게 압축

2. **후보 생성**
- `theme_db.json` 기반으로 테마별 소수 핵심 후보만 추림
- 각 종목에 `role=leader|momentum|watch` 메타를 부여
- `market_cap_bucket` 으로 대형주 편향을 조절

3. **반복 추천 패널티**
- 최근 추천/실패/손절 이력을 보고 점수 감점
- 완전 제외가 아니라 감점 방식으로 “지나친 반복”만 완화

4. **리더주 선별**
- 최근 3일/5일 상대강도
- 거래량/거래대금 증가
- 20일/60일 신고가 근접도
- 이동평균 추세
- 볼린저 밴드 위치
- 추격 위험

5. **최종 선별**
- 테마 강도는 후보 생성용
- 최종 선택은 `selection_score` 중심
- 같은 테마에서 리더 1개 + 보조 후보 1개만 유지

### 최종 선별에 반영되는 점수

| 점수 | 설명 |
|------|------|
| `theme_score` | 메가테마 강도 |
| `relative_strength_score` | 최근 3일/5일 상대 강도 |
| `volume_score` | 거래량/거래대금 평가 |
| `breakout_score` | 신고가 근접도 / 돌파 가능성 |
| `selection_score` | 역할 가중치, 반복 패널티 포함 최종 선별 점수 |

### 설계 의도

- “관련주냐”보다 “지금 실제로 가장 강한 종목이냐”를 더 크게 봅니다.
- 대형 대표주가 항상 우선되지 않도록 완화합니다.
- 반복 추천은 줄이되, 진짜 강한 종목은 완전히 배제하지 않습니다.

---

## 미국장 라이브 전환 절차

1. `US_READINESS_MODE=true` 로 주문 직전 검증만 수행
2. `run_agents.py --test` / `--status` 로 계좌/주문 경로 점검
3. 미국장 모의투자 자동매매 검증 시:

```env
KIS_MODE=virtual
US_STOCK_ENABLED=true
US_READINESS_MODE=false
US_REQUIRE_REAL_MODE=false
```

4. 실전 전환 시:

```env
KIS_MODE=real
US_READINESS_MODE=false
US_REQUIRE_REAL_MODE=true
US_MICRO_LIVE_MAX_NOTIONAL=300
```

5. 첫 1~3거래일은 소액 주문만 허용
6. 주문/체결/청산/로그 정합성 확인 후 예산 확대

### 미국장 readiness 에서 확인하는 것

- 실전 모드 여부
- 해외주식 주문 경로/TR ID
- 토큰 만료 자동 복구 가능 여부
- 미국장 주문 금액 제한
- 계좌/잔고 조회 가능 여부

---

## 텔레그램 알림

| 이벤트 | 알림 내용 |
|--------|-----------|
| 매수 | 종목명, 가격, 수량, 손절/익절선 |
| 매도 | 종목명, 매도가, 수익률, 수익금 |
| 스캔 결과 | 종목별 verdict 요약 |
| 수동 포지션 감지 | 외부 매수 종목 자동 동기화 |
| 시스템 오류 | 에러 메시지 |
| 미국 readiness | 주문 직전 검증 결과 |

---

## 주의사항

- `KIS_MODE=real` 전환 전 반드시 모의투자(`virtual`)로 충분히 검증할 것
- 모의투자 환경 URL: `https://openapivts.koreainvestment.com:29443`
- 실전 환경 URL: `https://openapi.koreainvestment.com:9443`
- KIS API 토큰 만료(`EGW00123`) 시 자동 재발급 후 재시도
- 잔고 조회 실패 시 `0원` 으로 덮어쓰지 않고 마지막 정상 잔고 유지
- KR/US 중 한 시장에 장애가 발생해도 다른 시장 상태는 분리 유지
- 미국장은 기본적으로 readiness mode 이므로, 별도 전환 전에는 실제 주문이 나가지 않음

---

## 📅 업데이트 이력

### 2026.04.25 — 1단계 안정화 + 2단계 종목 선택 로직 고도화

#### 🔧 주요 변경

**1. 런타임 안정화**

```
- all_slots 단일 포지션 원장 사용
- 주문 inflight lock / cooldown 추가
- KR / US 시장 상태 분리 저장
- last_good_balance / balance_sync_status 추가
- API 연속 장애 시 api_degraded_mode 전환
```

**2. KIS 장애 대응**

```
- EGW00123 토큰 만료 감지 시 자동 재발급 후 재시도
- 잔고 조회 실패 시 0원 저장 금지
- 주문 실패는 상태 미반영 원칙 유지
```

**3. 미국장 readiness mode**

```
- US_READINESS_MODE=true 기본값
- 미국장은 실제 주문 대신 주문 직전 검증과 추천 이력 기록만 수행
- 실전 전환 후 초기 주문금액 상한 지원
```

**4. 종목 선택 로직 개편**

```
- 테마 강도 계산식 재작성: 기사 수 + 출처 다양성 + 신선도 + 키워드 밀도
- theme_db 후보군을 leader / momentum / watch 관점으로 활용
- 최근 추천/실패/손절 이력 기반 반복 패널티 추가
- 테마 내 상대강도, 거래량, 신고가 근접도 기반 리더주 선별
- 기술 데이터 없는 종목은 상위 후보 제외
```

#### 🎯 기대 효과

- KR 모의투자에서 상태 꼬임, 중복 주문, 잘못된 잔고 동기화 감소
- US 라이브 전환 전 실제 주문 경로를 안전하게 점검 가능
- 특정 대표주 3개 반복 추천 완화
- 같은 테마에서도 “오늘 더 강한 종목”이 우선되도록 개선

### 2026.04.22 — 테마 감지 확대 및 장시간 처리 개선

#### 🔍 문제 진단

| 현상 | 원인 | 판단 |
|------|------|------|
| 밤 22:48에 스캔 결과 옴 | EDT(써머타임) 미국장 22:30 개장 직후 뉴스 급증 | ✅ 정상 동작 |
| 국장 스캔이 장마감에도 옴 | 스캔은 계속하되 매매만 차단하는 구조 | ✅ 정상 (단, 표시 부족) |
| 삼성전자/하이닉스/한미반도체만 반복 추천 | `theme_accumulator.py`의 `THEME_KEYWORDS`가 **6개 하드코딩** → AI/반도체 키워드 위주 감지 | ❌ 버그 |

#### 🔧 변경 사항

**1. `agents/theme_accumulator.py` — 테마 동적 로드**

```
기존: THEME_KEYWORDS = { 6개 하드코딩 테마 (AI_반도체, 방산, 배터리, 바이오, 로봇, 양자) }
개선: _load_theme_keywords() 함수로 theme_db.json 자동 로드
     → 하드코딩 6개 + theme_db.json 26개 = 총 32개 테마
     → JSON 로드 실패 시 기존 하드코딩 fallback 유지 (안전망)
```

**2. `data/theme_db.json` — 미국 티커 매핑**

```
각 테마에 stocks_us 필드 추가:
  반도체: NVDA, AMD, AVGO, INTC, TSMC, QCOM, MU, NXPI, ON, MRVL, ASML, LRCX, KLAC
  방산:   LMT, RTX, NOC, GD, LHX, TDG
  AI:     MSFT, GOOG, META, AMZN, CRM, PLTR, SNOW, AI, PATH, APLS, DDOG
  원전:   CEG, VST, NLR, TLN, CCJ
  우주:   SPCE, BA, LMT, RKLB, ASTS, RDW
  자율주행: TSLA, GOOG, MBLY, AVGO, IDSY
  바이오:  JNJ, PFE, MRNA, LLY, UNH, VEEV, DXCM, ISRG, TNDY
  ... 총 26개 테마에 US 티커 매핑
```

**3. `run_agents.py` — 장시간 처리 개선**

```
- EDT 썸머타임 정확 반영 (3~10월: 22:30~05:00 KST)
- 장마감 종목에 ⚠️장마감 표시 추가
- 장 시작/마감 시 Telegram 알림 추가
  🟢 🇰🇷 한국 장 시작! 매매 활성화
  🔴 🇰🇷 한국 장 마감. 매매 중단
  🟢 🇺🇸 미국 장 시작! (EDT 22:30~05:00) 매매 활성화
  🔴 🇺🇸 미국 장 마감. 매매 중단
```

#### 📊 개선 결과

```
기존: 6개 테마 감지 (AI_반도체, 방산, 배터리, 바이오, 로봇, 양자)
개선: 32개 테마 감지

주요 테마 (KR = 한국 종목, US = 미국 종목):
  반도체:     KR 18개 + US 13개
  AI:         KR 16개 + US 11개
  방산:       KR 10개 + US 6개
  바이오:     KR 12개 + US 9개
  이차전지:   KR 12개 + US 6개
  로봇:       KR 12개 + US 5개
  원전:       KR 10개 + US 5개
  우주:       KR 8개  + US 6개
  자율주행:   KR 9개  + US 5개
  엔터:       KR 10개 + US 5개
  금융:       KR 8개  + US 5개
  정유화학:   KR 6개  + US 4개
  태양광:     KR 5개  + US 4개
  수소:       KR 7개  + US 3개
  해운_물류:  KR 5개  + US 4개
  실리콘카바이드: KR 6개 + US 3개
  통신:       KR 3개  + US 3개
  디지털헬스케어: KR 5개 + US 3개
  탄소배출권: KR 5개  + US 4개
  철강소재:   KR 5개  + US 3개
  유통:       KR 6개  + US 4개
  증시테마:   KR 10개 + US 4개
  조선:       KR 4개  + US 0개
  건설:       KR 8개  + US 0개
  텅스텐:     KR 6개  + US 0개
  희토류:     KR 4개  + US 2개
  ... (+ 하드코딩 6개)
```

**4. `data/risk_state.json` — 리스크 상태 초기화**

```
- 연속 손실 카운터(consecutive_losses) 초기화
- 일일 손실 누적(daily_loss_pct) 리셋
- 마지막 거래 일시 갱신
- 이유: 이전 세션의 손실 기록이 남아있어 신규 매매가 차단될 수 있어서
```

**5. 웹 대시보드 — Flask 실시간 대시보드 추가**

```
접속 정보:
  로컬:   http://localhost:8501
  외부:   https://poly.dashmybot-home.com (Cloudflare 터널)

API 엔드포인트:
  /api/overview   — 계좌 잔고, 총자산, 봇 상태
  /api/positions  — 보유 종목 목록
  /api/trades     — 거래 내역
  /api/config     — 현재 설정 확인
```

**6. 전체 시스템 점검 & 재시작**

```
실행 확인:
  🤖 주식봇: 실행 중 (PID 자동 할당)
  📊 대시보드: 실행 중 (포트 8501)

start_bot.sh 명령어:
  ./start_bot.sh start    # 봇 + 대시보드 시작
  ./start_bot.sh stop     # 전체 중지
  ./start_bot.sh restart  # 재시작
  ./start_bot.sh status   # 상태 확인
```

#### 적용 방법

```bash
# 봇 재시작 시 자동 적용
cd stock_bot && ./start_bot.sh

# 개별 제어
./start_bot.sh start     # 시작
./start_bot.sh stop      # 중지
./start_bot.sh restart   # 재시작
./start_bot.sh status    # 상태 확인
```
