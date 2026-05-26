import pytest

from agent_orchestrator.actions import assert_session_action_allowed, build_session_actions, primary_action_from_registry
from agent_orchestrator import Orchestrator
from agent_orchestrator.planning import PlanStore, TeamOrchestrator
from test_support import start_approved_session, start_executed_session


def _session_payload(tmp_path, requirement: str = "Build a persisted plan artifact") -> dict[str, object]:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    return start_approved_session(team, requirement).to_dict()


def test_action_registry_exposes_execute_for_approved_session(tmp_path) -> None:
    payload = _session_payload(tmp_path)

    actions = {action["id"]: action for action in build_session_actions(payload)}
    primary = primary_action_from_registry(payload)

    assert actions["execute"]["enabled"] is True
    assert actions["execute"]["label"] == "开始执行"
    assert actions["execute"]["risk_level"] == "medium"
    assert actions["execute"]["confirmation_required"] is True
    assert "mode" in actions["execute"]["input_schema"]["properties"]
    assert actions["execute"]["state_changes"]
    assert actions["revise"]["enabled"] is False
    assert primary["primary_action"] == "execute"
    assert primary["primary_label"] == "开始执行"


def test_action_registry_rejects_unavailable_actions(tmp_path) -> None:
    payload = _session_payload(tmp_path)

    with pytest.raises(ValueError, match="不允许执行"):
        assert_session_action_allowed(payload, "approve")


def test_action_registry_validates_action_payloads(tmp_path) -> None:
    payload = _session_payload(tmp_path)

    with pytest.raises(ValueError, match="mode must be a string"):
        assert_session_action_allowed(payload, "execute", {"mode": 123})


def test_action_registry_maps_completed_session_to_inspect_execution(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    executed = start_executed_session(team, "Build a persisted plan artifact")

    actions = {action["id"]: action for action in build_session_actions(executed.to_dict())}
    primary = primary_action_from_registry(executed.to_dict())

    assert actions["inspect_execution"]["enabled"] is True
    assert primary["primary_action"] == "inspect_execution"
