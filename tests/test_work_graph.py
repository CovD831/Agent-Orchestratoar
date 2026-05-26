from agent_orchestrator import Orchestrator
from agent_orchestrator.planning import PlanStore, TeamOrchestrator
from agent_orchestrator.roles import DEFAULT_AGENT_ROLES, role_for_job_kind, role_for_work_unit_kind
from agent_orchestrator.work_graph import WorkGraphStore, graph_to_plan_tree, node_actions, next_executable_node, schedulable_nodes
from test_support import start_approved_session, start_executed_session, start_reviewed_session


def test_agent_role_registry_maps_job_and_work_unit_kinds() -> None:
    assert DEFAULT_AGENT_ROLES["lead"].layer == "decision"
    assert role_for_job_kind("implementation").id == "builder"
    assert role_for_job_kind("adversarial_review").id == "adversarial_reviewer"
    assert role_for_work_unit_kind("execution_run").id == "runtime"


def test_team_start_persists_initial_work_graph(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = start_reviewed_session(team, "Build a persisted plan artifact")
    graph = WorkGraphStore(tmp_path / "plans").read(session.id)

    assert graph.session_id == session.id
    assert graph.root_id == session.id
    assert {node.kind for node in graph.nodes} >= {"session", "subtask", "review_round", "adversarial_review"}
    assert any(node.linked_job_ids for node in graph.nodes if node.kind in {"review_round", "adversarial_review"})
    assert all(node.assigned_role for node in graph.nodes)
    assert any(node.allowed_actions for node in graph.nodes)
    assert all(node.next_action for node in graph.nodes)
    assert any(node.validation for node in graph.nodes if node.kind == "subtask")
    assert (tmp_path / "plans" / session.id / "work_graph.json").exists()


def test_work_graph_updates_execution_run_node_after_execute(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    team.orchestrator.run_store.__post_init__()
    executed = start_executed_session(team, "Build a persisted plan artifact")
    graph = WorkGraphStore(tmp_path / "plans").read(executed.id)

    assert executed.resume.linked_execution_run_id
    assert any(
        node.kind == "execution_run" and node.linked_run_id == executed.resume.linked_execution_run_id
        for node in graph.nodes
    )
    root = next(node for node in graph.nodes if node.id == executed.id)
    assert root.status == executed.status


def test_graph_to_plan_tree_preserves_related_jobs(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = start_reviewed_session(team, "Build a persisted plan artifact")

    tree = graph_to_plan_tree(WorkGraphStore(tmp_path / "plans").read(session.id))
    review_nodes = [node for node in tree["children"] if node["kind"] in {"review_round", "adversarial_review"}]

    assert tree["kind"] == "session"
    assert review_nodes
    assert any(node["related_agent_ids"] for node in review_nodes)
    assert all("allowed_actions" in node for node in review_nodes)


def test_work_graph_exposes_schedulable_nodes_and_node_actions(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = start_approved_session(team, "Build a persisted plan artifact")
    graph = WorkGraphStore(tmp_path / "plans").read(session.id)

    nodes = schedulable_nodes(graph)
    root = next(node for node in graph.nodes if node.id == session.id)

    assert nodes
    assert next_executable_node(graph) is not None
    assert "execute" in node_actions(root)
    assert all("blocked_by" in node for node in nodes)
