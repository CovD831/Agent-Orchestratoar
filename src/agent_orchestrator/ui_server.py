"""FastAPI app for the local Agent Team Console."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, pathlib, typing
# RESPONSIBILITY: Expose dashboard API routes and static console assets.
# MODULE: interface
# ---

from pathlib import Path
import json
import time
from typing import Any

from agent_orchestrator.ui_service import DashboardService, build_dashboard_service


def create_app(service: DashboardService | None = None) -> Any:
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import FileResponse
        from fastapi.responses import StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("Install UI dependencies with `pip install -e '.[ui]'` to run the dashboard.") from exc

    dashboard = service or build_dashboard_service()
    app = FastAPI(title="Agent Team Console")
    static_dir = _static_assets_dir()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return dashboard.health()

    @app.get("/api/agent-config")
    def get_agent_config() -> dict[str, object]:
        return dashboard.get_agent_config()

    @app.post("/api/agent-config")
    def update_agent_config(payload: dict[str, object] = Body(...)) -> dict[str, object]:
        return _call(lambda: dashboard.update_agent_config(payload), HTTPException)

    @app.get("/api/sessions")
    def list_sessions() -> dict[str, object]:
        return dashboard.list_sessions()

    @app.post("/api/sessions")
    def create_session(payload: dict[str, object] = Body(...)) -> dict[str, object]:
        return _call(lambda: dashboard.create_session(str(payload.get("requirement", ""))), HTTPException)

    @app.post("/api/sessions/ideate")
    def create_ideation_session(payload: dict[str, object] = Body(...)) -> dict[str, object]:
        return _call(lambda: dashboard.create_ideation_session(str(payload.get("requirement", ""))), HTTPException)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.get_session(session_id), HTTPException)

    @app.get("/api/events")
    def list_events() -> dict[str, object]:
        return dashboard.list_events()

    @app.get("/api/sessions/{session_id}/events")
    def list_session_events(session_id: str) -> dict[str, object]:
        return dashboard.list_session_events(session_id)

    @app.get("/api/memory")
    def list_memory() -> dict[str, object]:
        return dashboard.list_memory()

    @app.get("/api/memory/search")
    def search_memory(q: str = "", session_id: str | None = None) -> dict[str, object]:
        return dashboard.search_memory(q, session_id=session_id)

    @app.get("/api/sessions/{session_id}/memory")
    def list_session_memory(session_id: str) -> dict[str, object]:
        return dashboard.list_session_memory(session_id)

    @app.get("/api/messages")
    def list_messages() -> dict[str, object]:
        return dashboard.list_messages()

    @app.get("/api/sessions/{session_id}/messages")
    def list_session_messages(session_id: str) -> dict[str, object]:
        return dashboard.list_session_messages(session_id)

    @app.get("/api/stream")
    def stream_events(once: bool = False) -> Any:
        return StreamingResponse(_sse_stream(dashboard, once=once), media_type="text/event-stream")

    @app.get("/api/sessions/{session_id}/stream")
    def stream_session_events(session_id: str, once: bool = False) -> Any:
        return StreamingResponse(
            _sse_stream(dashboard, session_id=session_id, once=once),
            media_type="text/event-stream",
        )

    @app.post("/api/sessions/{session_id}/revise")
    def revise_session(session_id: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
        closed_gap_ids = payload.get("closed_gap_ids", [])
        gap_ids = [str(item) for item in closed_gap_ids] if isinstance(closed_gap_ids, list) else []
        return _call(lambda: dashboard.revise_session(session_id, summary=str(payload.get("summary", "")), closed_gap_ids=gap_ids), HTTPException)

    @app.post("/api/sessions/{session_id}/chat")
    def chat_with_lead(session_id: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
        return _call(lambda: dashboard.chat_with_lead(session_id, message=str(payload.get("message", ""))), HTTPException)

    @app.post("/api/sessions/{session_id}/draft-ready")
    def mark_draft_ready(session_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.mark_draft_ready(session_id), HTTPException)

    @app.post("/api/sessions/{session_id}/submit-review")
    def submit_draft_for_review(session_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.submit_draft_for_review(session_id), HTTPException)

    @app.post("/api/sessions/{session_id}/approve")
    def approve_session(session_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.approve_session(session_id), HTTPException)

    @app.post("/api/sessions/{session_id}/execute")
    def execute_session(session_id: str, payload: dict[str, object] = Body(default={})) -> dict[str, object]:
        mode = payload.get("mode") if isinstance(payload, dict) else None
        return _call(lambda: dashboard.execute_session(session_id, mode=str(mode) if mode else None), HTTPException)

    @app.post("/api/sessions/{session_id}/retry-review")
    def retry_review(session_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.retry_review(session_id), HTTPException)

    @app.post("/api/sessions/{session_id}/retry-adversarial-review")
    def retry_adversarial_review(session_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.retry_adversarial_review(session_id), HTTPException)

    @app.post("/api/sessions/{session_id}/resume")
    def resume_session(session_id: str, payload: dict[str, object] = Body(default={})) -> dict[str, object]:
        return _call(lambda: dashboard.resume_session(session_id, apply=bool(payload.get("apply", False))), HTTPException)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.get_run(run_id), HTTPException)

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, object]:
        return dashboard.list_jobs()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.get_job(job_id), HTTPException)

    @app.get("/api/jobs/{job_id}/log")
    def get_job_log(job_id: str) -> dict[str, object]:
        return dashboard.get_job_log(job_id)

    @app.get("/api/jobs/{job_id}/terminal/snapshot")
    def get_job_terminal_snapshot(job_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.get_job_terminal_snapshot(job_id), HTTPException)

    @app.post("/api/jobs/{job_id}/terminal/input")
    def send_job_terminal_input(job_id: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
        return _call(lambda: dashboard.send_job_terminal_input(job_id, str(payload.get("message", ""))), HTTPException)

    @app.post("/api/jobs/{job_id}/terminal/reconnect")
    def reconnect_job_terminal(job_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.reconnect_job_terminal(job_id), HTTPException)

    @app.post("/api/jobs/{job_id}/send")
    def send_job(job_id: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
        return _call(lambda: dashboard.send_job(job_id, str(payload.get("message", ""))), HTTPException)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        return _call(lambda: dashboard.cancel_job(job_id), HTTPException)

    return app


def _call(fn: Any, http_exception: Any) -> Any:
    try:
        return fn()
    except KeyError as exc:
        raise http_exception(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise http_exception(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc


def _static_assets_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    frontend_dist = repo_root / "ui_frontend" / "dist"
    if (frontend_dist / "index.html").exists():
        return frontend_dist
    return Path(__file__).with_name("ui_static")


def _sse_stream(
    dashboard: DashboardService,
    *,
    session_id: str | None = None,
    once: bool = False,
    poll_interval: float = 1.0,
    heartbeat_interval: float = 15.0,
) -> Any:
    seen: set[tuple[str, str]] = set()
    last_heartbeat = time.monotonic()
    while True:
        emitted = False
        for event_name, item in _stream_records(dashboard, session_id=session_id):
            key = _stream_key(event_name, item)
            if key in seen:
                continue
            seen.add(key)
            emitted = True
            yield _sse_frame(event_name, item)
        if once:
            break
        now = time.monotonic()
        if not emitted and now - last_heartbeat >= heartbeat_interval:
            last_heartbeat = now
            yield _sse_frame("heartbeat", {"ok": True, "session_id": session_id})
        time.sleep(poll_interval)


def _stream_records(dashboard: DashboardService, *, session_id: str | None = None) -> list[tuple[str, dict[str, object]]]:
    events_payload = dashboard.list_session_events(session_id) if session_id else dashboard.list_events()
    messages_payload = dashboard.list_session_messages(session_id) if session_id else dashboard.list_messages()
    jobs_payload = dashboard.list_jobs()
    records: list[tuple[str, dict[str, object]]] = []
    records.extend(("orchestration_event", event) for event in _items(events_payload.get("events")))
    records.extend(("team_message", message) for message in _items(messages_payload.get("messages")))
    records.extend(
        ("job_update", job)
        for job in _items(jobs_payload.get("jobs"))
        if session_id is None or job.get("session_id") == session_id or str(job.get("task_id") or "").startswith(session_id)
    )
    return records


def _items(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _stream_key(event_name: str, item: dict[str, object]) -> tuple[str, str]:
    if event_name == "job_update":
        return (
            event_name,
            "|".join(
                str(item.get(key) or "")
                for key in ("id", "status", "updated_at", "last_seen_at", "last_log_excerpt")
            ),
        )
    return event_name, str(item.get("id") or json.dumps(item, sort_keys=True, ensure_ascii=False))


def _sse_frame(event_name: str, payload: dict[str, object]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
