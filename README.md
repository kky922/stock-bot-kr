# Stock Bot KR

한국투자증권 KIS API를 사용하는 한국·미국 주식 자동매매 연구 프로젝트입니다.
뉴스와 테마 후보를 수집하고 기술적 조건, 주문 한도, 계좌 상태를 통과한 신호만
모의 주문 또는 명시적으로 허용된 주문 경로로 전달합니다.

## 기능

- 한국·미국 시장 후보 수집과 기술 지표 분석
- KIS 모의투자 기본값과 미국장 readiness 모드
- 중복 주문 잠금, 재진입 대기시간, 일일 손실 제한
- 포지션 동기화, 손절·익절 모니터, Telegram 알림
- Flask 대시보드와 14개 이상의 회귀 테스트

## 설치

```bash
git clone https://github.com/kky922/stock-bot-kr.git
cd stock-bot-kr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 설정

필수 환경변수:

```env
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=...
KIS_ACCOUNT_PRODUCT=01
KIS_MODE=virtual
```

선택 기능은 `ZAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`로 활성화합니다.
첫 실행은 반드시 `KIS_MODE=virtual`로 진행하세요.

## 실행

```bash
# 설정과 연결 확인
python run_agents.py --test

# 주문 없이 현재 상태 확인
python run_agents.py --status

# 파이프라인 1회 실행
python run_agents.py --once

# 스케줄 루프 실행
python run_agents.py

# 대시보드
python dashboard.py
```

실제 API 없이 핵심 로직만 확인하려면 테스트를 실행합니다.

## 안전장치

- 저장소 기본 정책은 한국장 모의투자와 미국장 readiness입니다.
- 주문 전 계좌, 시장, 예산, 수량, 중복 주문 여부를 검증합니다.
- API 장애 시 마지막 정상 잔고를 무기한 신뢰하지 않고 거래를 제한합니다.
- 런타임 상태와 거래 기록은 Git에서 제외됩니다.

## 테스트

```bash
pytest -q
```

## 주의사항

이 프로젝트는 학습과 연구 목적이며 투자 자문이 아닙니다. 자동매매에는 API 장애,
체결 지연, 가격 급변과 원금 손실 위험이 있습니다. 실계좌 전환 전 모의투자 결과와
주문 한도를 직접 검증해야 합니다.

과거 변경 배경은 [docs/history.md](docs/history.md)에 요약되어 있습니다.

## 라이선스

MIT
