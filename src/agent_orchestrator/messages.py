"""Structured agent-to-agent messages for team orchestration."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, json, pathlib, typing, uuid
# RESPONSIBILITY: Persist and route structured communication between agent roles.
# MODULE: interface
# ---

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_orchestrator.jobs import now_iso


@dataclass(frozen=True, slots=True)
class TeamMessage:
    id: str
    session_id: str
    work_unit_id: str | None
    from_role: str
    to_role: str
    message_type: str
    content: str
    payload: dict[str, Any] = field(default_factory=dict)
    thread: str = "main"
    requires_response: bool = False
    status: str = "sent"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "work_unit_id": self.work_unit_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "message_type": self.message_type,
            "content": self.content,
            "payload": self.payload,
            "thread": self.thread,
            "requires_response": self.requires_response,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TeamMessage":
        return cls(
            id=str(data.get("id") or f"msg-{uuid4().hex[:8]}"),
            session_id=str(data.get("session_id") or ""),
            work_unit_id=data.get("work_unit_id") if isinstance(data.get("work_unit_id"), str) else None,
            from_role=str(data.get("from_role") or "unknown"),
            to_role=str(data.get("to_role") or "unknown"),
            message_type=str(data.get("message_type") or "note"),
            content=str(data.get("content") or ""),
            payload=dict(data.get("payload", {})) if isinstance(data.get("payload"), dict) else {},
            thread=str(data.get("thread") or _thread_from_payload(dict(data.get("payload", {})) if isinstance(data.get("payload"), dict) else {})),
            requires_response=bool(data.get("requires_response", False)),
            status=str(data.get("status") or "sent"),
            created_at=str(data.get("created_at") or now_iso()),
        )


@dataclass(slots=True)
class MessageStore:
    root: Path | str = ".agent_orchestrator/messages"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, message: TeamMessage) -> TeamMessage:
        with self._messages_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        return message

    def create(
        self,
        *,
        session_id: str,
        from_role: str,
        to_role: str,
        message_type: str,
        content: str,
        work_unit_id: str | None = None,
        payload: dict[str, Any] | None = None,
        thread: str = "main",
        requires_response: bool = False,
        status: str = "sent",
    ) -> TeamMessage:
        return self.append(
            TeamMessage(
                id=f"msg-{uuid4().hex[:10]}",
                session_id=session_id,
                work_unit_id=work_unit_id,
                from_role=from_role,
                to_role=to_role,
                message_type=message_type,
                content=content,
                payload=payload or {},
                thread=thread,
                requires_response=requires_response,
                status=status,
            )
        )

    def query(
        self,
        *,
        session_id: str | None = None,
        from_role: str | None = None,
        to_role: str | None = None,
        message_type: str | None = None,
        work_unit_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        messages = self._read_all()
        if session_id is not None:
            messages = [message for message in messages if message.session_id == session_id]
        if from_role is not None:
            messages = [message for message in messages if message.from_role == from_role]
        if to_role is not None:
            messages = [message for message in messages if message.to_role == to_role]
        if message_type is not None:
            messages = [message for message in messages if message.message_type == message_type]
        if work_unit_id is not None:
            messages = [message for message in messages if message.work_unit_id == work_unit_id]
        return [message.to_dict() for message in messages[-limit:]][::-1]

    def list_for_session(self, session_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        return self.query(session_id=session_id, limit=limit)

    def list_threads_for_session(self, session_id: str, *, limit: int = 200) -> dict[str, list[dict[str, object]]]:
        threads: dict[str, list[dict[str, object]]] = {}
        for message in self.query(session_id=session_id, limit=limit):
            threads.setdefault(str(message.get("thread") or "main"), []).append(message)
        return threads

    def list_for_role(self, session_id: str, role: str, *, direction: str = "both", limit: int = 100) -> list[dict[str, object]]:
        if direction == "inbox":
            return self.query(session_id=session_id, to_role=role, limit=limit)
        if direction == "outbox":
            return self.query(session_id=session_id, from_role=role, limit=limit)
        messages = [
            message
            for message in self._read_all()
            if message.session_id == session_id and role in {message.from_role, message.to_role}
        ]
        return [message.to_dict() for message in messages[-limit:]][::-1]

    @property
    def _messages_path(self) -> Path:
        return self.root / "messages.jsonl"

    def _read_all(self) -> list[TeamMessage]:
        path = self._messages_path
        if not path.exists():
            return []
        messages: list[TeamMessage] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                messages.append(TeamMessage.from_dict(payload))
        return messages


@dataclass(slots=True)
class MessageRouter:
    store: MessageStore

    def build_review_request(
        self,
        *,
        session_id: str,
        to_role: str,
        content: str,
        work_unit_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TeamMessage:
        return self.store.create(
            session_id=session_id,
            work_unit_id=work_unit_id,
            from_role="lead",
            to_role=to_role,
            message_type="review_request",
            content=content,
            payload=payload,
            thread="review",
            requires_response=True,
        )

    def build_review_result(
        self,
        *,
        session_id: str,
        from_role: str,
        content: str,
        work_unit_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TeamMessage:
        return self.store.create(
            session_id=session_id,
            work_unit_id=work_unit_id,
            from_role=from_role,
            to_role="lead",
            message_type="review_result",
            content=content,
            payload=payload,
            thread="review",
            requires_response=False,
        )

    def build_handoff(
        self,
        *,
        session_id: str,
        from_role: str,
        to_role: str,
        content: str,
        work_unit_id: str | None = None,
        payload: dict[str, Any] | None = None,
        requires_response: bool = False,
    ) -> TeamMessage:
        return self.store.create(
            session_id=session_id,
            work_unit_id=work_unit_id,
            from_role=from_role,
            to_role=to_role,
            message_type="handoff",
            content=content,
            payload=payload,
            thread=_thread_from_payload(payload or {}),
            requires_response=requires_response,
        )


def _thread_from_payload(payload: dict[str, Any]) -> str:
    explicit = payload.get("thread")
    if isinstance(explicit, str) and explicit:
        return explicit
    round_type = str(payload.get("round_type") or "")
    artifact_kind = str(payload.get("artifact_kind") or "")
    if "review" in round_type or artifact_kind == "review_findings":
        return "review"
    if artifact_kind in {"execution_result", "runtime_handoff"} or payload.get("run_id"):
        return "rescue" if payload.get("status") == "blocked" else "main"
    return "main"
