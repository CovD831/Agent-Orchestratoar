"""Lightweight append-only memory records for orchestration evidence."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, json, pathlib, typing, uuid
# RESPONSIBILITY: Persist queryable evidence, action, and postmortem records.
# MODULE: interface
# ---

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_orchestrator.jobs import now_iso


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    namespace: str
    session_id: str
    role: str | None
    provider: str | None
    record_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "role": self.role,
            "provider": self.provider,
            "record_type": self.record_type,
            "summary": self.summary,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "MemoryRecord":
        return cls(
            id=str(data.get("id") or f"memory-{uuid4().hex[:8]}"),
            namespace=str(data.get("namespace") or "general"),
            session_id=str(data.get("session_id") or ""),
            role=data.get("role") if isinstance(data.get("role"), str) else None,
            provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
            record_type=str(data.get("record_type") or "note"),
            summary=str(data.get("summary") or ""),
            payload=dict(data.get("payload", {})) if isinstance(data.get("payload"), dict) else {},
            created_at=str(data.get("created_at") or now_iso()),
        )


@dataclass(slots=True)
class MemoryStore:
    root: Path | str = ".agent_orchestrator/memory"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        namespace: str,
        session_id: str,
        record_type: str,
        summary: str,
        role: str | None = None,
        provider: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            id=f"memory-{uuid4().hex[:10]}",
            namespace=namespace,
            session_id=session_id,
            role=role,
            provider=provider,
            record_type=record_type,
            summary=summary,
            payload=payload or {},
        )
        with self._memory_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record

    def query(
        self,
        *,
        session_id: str | None = None,
        namespace: str | None = None,
        record_type: str | None = None,
        provider: str | None = None,
        role: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        records = self._read_all()
        if session_id is not None:
            records = [record for record in records if record.session_id == session_id]
        if namespace is not None:
            records = [record for record in records if record.namespace == namespace]
        if record_type is not None:
            records = [record for record in records if record.record_type == record_type]
        if provider is not None:
            records = [record for record in records if record.provider == provider]
        if role is not None:
            records = [record for record in records if record.role == role]
        return [record.to_dict() for record in records[-limit:]][::-1]

    def search(self, query: str, *, session_id: str | None = None, limit: int = 5) -> list[dict[str, object]]:
        terms = {term.lower() for term in query.replace("_", " ").split() if term.strip()}
        if not terms:
            return []
        candidates = self._read_all()
        if session_id is not None:
            candidates = [record for record in candidates if record.session_id == session_id]
        scored: list[tuple[int, MemoryRecord]] = []
        for record in candidates:
            haystack = f"{record.namespace} {record.record_type} {record.summary} {json.dumps(record.payload, ensure_ascii=False)}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [record.to_dict() for _, record in scored[:limit]]

    @property
    def _memory_path(self) -> Path:
        return self.root / "memory.jsonl"

    def _read_all(self) -> list[MemoryRecord]:
        path = self._memory_path
        if not path.exists():
            return []
        records: list[MemoryRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(MemoryRecord.from_dict(payload))
        return records


@dataclass(slots=True)
class KnowledgeStore:
    root: Path | str = ".agent_orchestrator/knowledge"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        session_id: str,
        artifact_type: str,
        summary: str,
        role: str = "lead",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        record = {
            "id": f"knowledge-{uuid4().hex[:10]}",
            "session_id": session_id,
            "artifact_type": artifact_type,
            "role": role,
            "summary": summary,
            "payload": payload or {},
            "created_at": now_iso(),
        }
        path = self._path_for_type(artifact_type)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def query(self, *, session_id: str | None = None, artifact_type: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        paths = [self._path_for_type(artifact_type)] if artifact_type else sorted(self.root.glob("*.jsonl"))
        for path in paths:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if session_id is not None and payload.get("session_id") != session_id:
                    continue
                records.append(payload)
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records[:limit]

    def _path_for_type(self, artifact_type: str | None) -> Path:
        safe_type = (artifact_type or "notes").replace("/", "_")
        return self.root / f"{safe_type}.jsonl"
