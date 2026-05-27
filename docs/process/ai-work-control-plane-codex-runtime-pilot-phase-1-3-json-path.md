# AI Work Control Plane Codex Runtime Pilot Phase 1-3: JSON Path

## Goal

Implement the first opt-in Codex runtime pilot path:

```text
codex exec --json
  -> JSONL parser
  -> optional final-message artifact
  -> job parsed payload
  -> provider-owned ProviderSessionRef
  -> ProviderSessionSnapshot
```

## Implementation

- `CodexCliAdapter` keeps the default `codex exec` command unchanged.
- Request metadata can enable the pilot:

```json
{
  "codex_pilot": {
    "json_events": true,
    "output_last_message": "/path/to/final-message.txt"
  }
}
```

- When enabled, the adapter adds:

```text
--json
--output-last-message <path>
```

- The parser records:
  - event count
  - event type counts
  - last 20 JSON events
  - malformed event count
  - final message
  - observed session/thread refs
  - provider-reported usage when present

## Boundary

- `ProviderSessionRef` is marked `provider_owned: true`.
- `continuation_guarantee` remains `provider_owned`.
- Token/cost remains placeholder unless Codex reports usage directly.
- Send/cancel support is unchanged and remains operation-receipt based.

## Validation

```bash
pytest tests/test_command.py -q
pytest tests/test_control_plane.py::test_provider_session_snapshot_exposes_provider_owned_ref -q
```

Observed targeted validation:

- `pytest tests/test_command.py -q`: 27 passed.
- `pytest tests/test_command.py tests/test_control_plane.py tests/test_cli.py -q`: 131 passed.
- `pytest tests/test_control_plane.py tests/test_cli.py tests/test_team.py -q`: 218 passed.
- `PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance`: passed, blocking false.

Final convergence validation:

- `pytest`: 419 passed.
- `PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance`: passed, blocking false.
- `PYTHONPATH=src python -m agent_orchestrator.cli team setup --runtime command --format json`: exit 0, `release_readiness.ready: true`.
- `PYTHONPATH=src python -m agent_orchestrator.cli team workspace-status --format json`: exit 0.
- `PYTHONPATH=src python -m agent_orchestrator.cli team evidence-gates --format json`: exit 0, `status: ready`.

## Result

Phase 1-3 implementation is complete:

- The default Codex command path remains unchanged.
- Codex pilot metadata can opt into `codex exec --json`.
- `--output-last-message` can be configured as an artifact path.
- JSONL output is parsed into `agent_orchestrator.codex_exec_json.v1`.
- Provider-owned refs are stored in `agent_orchestrator.provider_session_ref.v1`.
- `ProviderSessionSnapshot` exposes `provider_session_ref` read-only.
- Tests use fake runners and local fixtures only.
