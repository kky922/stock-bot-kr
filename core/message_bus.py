"""
Agent 간 메시지 전달을 위한 Message Bus.
threading.Queue 기반으로 각 에이전트가 비동기적으로 통신합니다.
"""

import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    """메시지 타입 정의."""
    MARKET_SIGNAL = "market_signal"          # Market Scout → Technical Analyst
    TECHNICAL_REPORT = "technical_report"    # Technical Analyst → Risk Manager
    RISK_DECISION = "risk_decision"          # Risk Manager → Trade Executor
    EXECUTION_REPORT = "execution_report"    # Trade Executor → Monitor
    FEEDBACK_SIGNAL = "feedback_signal"      # Monitor → Orchestrator
    EXIT_SIGNAL = "exit_signal"              # Monitor → Trade Executor
    SYSTEM_COMMAND = "system_command"        # Orchestrator → All
    STATUS_UPDATE = "status_update"          # Any → Orchestrator


@dataclass
class AgentMessage:
    """에이전트 간 전달되는 메시지."""
    msg_type: str
    sender: str
    data: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    priority: int = 0  # 0=normal, 1=high, 2=critical
    msg_id: str = ""

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = f"{self.sender}_{int(time.time() * 1000)}"


class MessageBus:
    """에이전트 간 메시지 전달 버스."""

    def __init__(self):
        self._queues: Dict[str, queue.Queue] = {}
        self._subscribers: Dict[str, List[str]] = defaultdict(list)
        self._history: List[AgentMessage] = []
        self._lock = threading.Lock()
        self._max_history = 1000

    def register(self, agent_name: str, maxsize: int = 100) -> queue.Queue:
        """에이전트 등록 및 전용 큐 생성."""
        q = queue.Queue(maxsize=maxsize)
        self._queues[agent_name] = q
        logger.info("📡 MessageBus: '%s' 등록됨", agent_name)
        return q

    def subscribe(self, agent_name: str, msg_type: str):
        """특정 메시지 타입 구독."""
        if agent_name not in self._subscribers[msg_type]:
            self._subscribers[msg_type].append(agent_name)

    def send(self, message: AgentMessage, target: Optional[str] = None):
        """특정 에이전트에게 메시지 전송."""
        with self._lock:
            self._history.append(message)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        if target and target in self._queues:
            try:
                self._queues[target].put_nowait(message)
            except Exception:
                logger.warning("⚠️ %s 큐 가득참 — 메시지 드랍: %s", target, message.msg_type)
        else:
            subscribers = self._subscribers.get(message.msg_type, [])
            for sub_name in subscribers:
                if sub_name == message.sender:
                    continue
                if sub_name in self._queues:
                    try:
                        self._queues[sub_name].put_nowait(message)
                    except Exception:
                        pass

    def broadcast(self, message: AgentMessage):
        """모든 등록된 에이전트에게 브로드캐스트."""
        for agent_name, q in self._queues.items():
            if agent_name == message.sender:
                continue
            try:
                q.put_nowait(message)
            except Exception:
                pass

    def receive(self, agent_name: str, timeout: float = 1.0) -> Optional[AgentMessage]:
        """에이전트 큐에서 메시지 수신."""
        q = self._queues.get(agent_name)
        if not q:
            return None
        try:
            return q.get(timeout=timeout)
        except Exception:
            return None

    def get_history(self, msg_type: Optional[str] = None, limit: int = 50) -> List[AgentMessage]:
        """메시지 이력 조회."""
        with self._lock:
            if msg_type:
                return [m for m in self._history if m.msg_type == msg_type][-limit:]
            return self._history[-limit:]