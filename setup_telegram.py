"""
텔레그램 봇 설정 도우미.
CHAT_ID를 자동으로 감지하고 .env 파일을 업데이트합니다.

사용법:
  python3 setup_telegram.py

1. 텔레그램에서 @Stock_sky_bot 에게 /start 를 보내세요
2. 이 스크립트가 자동으로 CHAT_ID를 감지합니다
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{TOKEN}"


def test_token():
    """봇 토큰 유효성 검사."""
    print("🔍 봇 토큰 확인 중...")
    resp = requests.get(f"{API_BASE}/getMe", timeout=10)
    data = resp.json()
    if data.get("ok"):
        bot = data["result"]
        print(f"✅ 봇 확인: @{bot['username']} ({bot['first_name']})")
        return True
    else:
        print(f"❌ 봇 토큰 오류: {data.get('description', '알 수 없음')}")
        return False


def get_chat_id(timeout=60):
    """사용자가 /start 를 보낼 때까지 대기 후 CHAT_ID 감지."""
    print(f"\n📱 텔레그램에서 @Stock_sky_bot 에게 /start 를 보내주세요!")
    print(f"⏳ {timeout}초 대기 중...\n")

    offset = 0
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            resp = requests.get(
                f"{API_BASE}/getUpdates",
                params={"offset": offset, "timeout": 5},
                timeout=10,
            )
            data = resp.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    if "message" in update:
                        msg = update["message"]
                        chat = msg.get("chat", {})
                        chat_id = chat.get("id")
                        chat_type = chat.get("type", "unknown")
                        username = chat.get("username", "")
                        first_name = chat.get("first_name", "")
                        text = msg.get("text", "")

                        print(f"📨 메시지 수신: \"{text}\"")
                        print(f"   채팅: {first_name} (@{username}) [type={chat_type}]")
                        print(f"   CHAT_ID: {chat_id}")

                        return chat_id, chat_type, username

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"⚠️ 오류: {e}")

        # 진행 표시
        elapsed = int(time.time() - start_time)
        print(f"  ... 대기 중 ({elapsed}/{timeout}초)", end="\r")
        sys.stdout.flush()

    return None, None, None


def update_env(chat_id):
    """.env 파일의 CHAT_ID 업데이트."""
    env_file = ROOT_DIR / ".env"
    lines = []

    if env_file.exists():
        with open(env_file, encoding="utf-8") as f:
            lines = f.readlines()

    # TELEGRAM_CHAT_ID 라인 찾아서 업데이트
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("TELEGRAM_CHAT_ID="):
            lines[i] = f"TELEGRAM_CHAT_ID={chat_id}\n"
            found = True
            break

    if not found:
        # TELEGRAM_BOT_TOKEN 다음에 추가
        for i, line in enumerate(lines):
            if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
                lines.insert(i + 1, f"TELEGRAM_CHAT_ID={chat_id}\n")
                found = True
                break
        if not found:
            lines.append(f"TELEGRAM_CHAT_ID={chat_id}\n")

    with open(env_file, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"✅ .env 파일 업데이트 완료: TELEGRAM_CHAT_ID={chat_id}")


def send_test_message(chat_id):
    """테스트 메시지 전송."""
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "🧪 stock_bot 텔레그램 연결 성공!\n\n✅ 봇이 정상적으로 연결되었습니다.\n📊 /start 로 명령어를 확인하세요.",
        },
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print("✅ 테스트 메시지 전송 성공! 텔레그램을 확인하세요.")
        return True
    else:
        print(f"❌ 메시지 전송 실패: {data.get('description', '')}")
        return False


def main():
    print("=" * 50)
    print("🤖 stock_bot 텔레그램 설정 도우미")
    print("=" * 50)

    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN 이 .env에 없습니다.")
        sys.exit(1)

    # 1. 토큰 확인
    if not test_token():
        sys.exit(1)

    # 2. CHAT_ID 감지
    chat_id, chat_type, username = get_chat_id(timeout=120)

    if chat_id is None:
        print("\n❌ 시간 초과 — 메시지를 받지 못했습니다.")
        print("   텔레그램에서 @Stock_sky_bot 을 찾아 /start 를 보낸 후 다시 실행하세요.")
        sys.exit(1)

    print(f"\n🎯 감지된 CHAT_ID: {chat_id} (type: {chat_type})")

    # 3. .env 업데이트
    update_env(chat_id)

    # 4. 테스트 메시지
    print("\n📤 테스트 메시지 전송 중...")
    if send_test_message(chat_id):
        print("\n🎉 텔레그램 연결 완료! 이제 python3 main.py 로 봇을 시작하세요.")
    else:
        print("\n⚠️ 연결은 되었으나 메시지 전송에 실패했습니다.")


if __name__ == "__main__":
    main()