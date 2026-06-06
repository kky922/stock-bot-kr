"""
에이전트 공통 베이스 클래스.
모든 에이전트는 이 클래스를 상속받아 구현됩니다.
"""

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.message_bus import MessageBus, AgentMessage, MessageType

logger = logging.getLogger(__name__)


class AgentStatus:
    """에이전트 상태."""
    IDLE = "idle"
    RUNNING = "running"
    PROCESSING = "processing"
    ERROR = "error"
    STOPPED = "stopped"


class BaseAgent:
    """모든 에이전트의 베이스 클래스."""

    def __init__(self, name: str, message_bus: MessageBus):
        self.name = name
        self.bus = message_bus
        self.status = AgentStatus.IDLE
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._queue = self.bus.register(name)
        self._loop_interval = 1.0  # 기본 루프 간격 (초)
        self._last_activity = ""
        self._error_count = 0
        self._max_errors = 10

        logger.info("🤖 Agent '%s' 초기화됨", self.name)

    def start(self):
        """에이전트 시작 (별도 스레드)."""
        if self._running:
            return
        self._running = True
        self.status = AgentStatus.RUNNING
        self._thread = threading.Thread(target=self._run_loop, name=f"agent_{self.name}", daemon=True)
        self._thread.start()
        logger.info("▶️ Agent '%s' 시작됨", self.name)

    def stop(self):
        """에이전트 정지."""
        self._running = False
        self.status = AgentStatus.STOPPED
        logger.info("⏹️ Agent '%s' 정지됨", self.name)

    def _run_loop(self):
        """메인 루프 — 서브클래스에서 process()를 주기적으로 호출."""
        while self._running:
            try:
                self.status = AgentStatus.RUNNING
                self.process()
                time.sleep(self._loop_interval)
            except Exception as e:
                self._error_count += 1
                self.status = AgentStatus.ERROR
                logger.error("❌ Agent '%s' 오류 (%d/%d): %s",
                             self.name, self._error_count, self._max_errors, e)
                if self._error_count >= self._max_errors:
                    logger.critical("🚨 Agent '%s' 오류 한계 초과 — 정지", self.name)
                    self._running = False
                    break
                time.sleep(5)

    def process(self):
        """메인 처리 로직 — 서브클래스에서 구현."""
        raise NotImplementedError

    def send_message(self, msg_type: str, data: Dict, target: Optional[str] = None):
        """다른 에이전트에게 메시지 전송."""
        msg = AgentMessage(
            msg_type=msg_type,
            sender=self.name,
            data=data,
        )
        self.bus.send(msg, target=target)
        self._last_activity = f"sent:{msg_type}"

    def receive_message(self, timeout: float = 1.0) -> Optional[AgentMessage]:
        """메시지 수신."""
        msg = self.bus.receive(self.name, timeout=timeout)
        if msg:
            self._last_activity = f"recv:{msg.msg_type}"
        return msg

    def get_status(self) -> Dict[str, Any]:
        """에이전트 상태 정보."""
        return {
            "name": self.name,
            "status": self.status,
            "running": self._running,
            "last_activity": self._last_activity,
            "error_count": self._error_count,
            "queue_size": self.bus._queues.get(self.name, queue.Queue()).qsize() if hasattr(self.bus, '_queues') else 0,
        }