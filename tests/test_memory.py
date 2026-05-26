from agent_orchestrator import Orchestrator
from agent_orchestrator.memory import KnowledgeStore, MemoryStore
from agent_orchestrator.planning import PlanStore, TeamOrchestrator


def test_memory_store_appends_and_queries_records(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory")

    store.append(
        namespace="plan_session",
        session_id="plan-1",
        record_type="session_snapshot",
        role="lead",
        provider="decision_core",
        summary="snapshot",
    )
    store.append(
        namespace="operator_action",
        session_id="plan-1",
        record_type="action",
        role="lead",
        provider="dashboard",
        summary="execute",
    )

    assert len(store.query(session_id="plan-1")) == 2
    assert store.query(namespace="operator_action")[0]["summary"] == "execute"
    assert store.query(provider="decision_core")[0]["record_type"] == "session_snapshot"


def test_memory_store_search_scores_summary_and_payload(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.append(
        namespace="postmortem",
        session_id="plan-1",
        record_type="postmortem",
        summary="Dashboard execution failed because provider auth expired",
        payload={"provider": "claude"},
    )
    store.append(
        namespace="plan_session",
        session_id="plan-2",
        record_type="session_snapshot",
        summary="Unrelated work",
    )

    results = store.search("dashboard provider auth")

    assert len(results) == 1
    assert results[0]["session_id"] == "plan-1"


def test_plan_store_writes_session_snapshot_memory(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = team.start("Build a persisted plan artifact")
    records = MemoryStore(tmp_path / "memory").query(session_id=session.id)

    assert records
    assert any(record["record_type"] == "session_snapshot" for record in records)


def test_knowledge_store_appends_type_scoped_jsonl(tmp_path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")

    store.append(session_id="plan-1", artifact_type="decisions", summary="approved", payload={"status": "approved"})
    store.append(session_id="plan-1", artifact_type="lessons", summary="validated")

    records = store.query(session_id="plan-1")

    assert {record["artifact_type"] for record in records} == {"decisions", "lessons"}
    assert (tmp_path / "knowledge" / "decisions.jsonl").exists()
