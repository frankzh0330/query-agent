"""Data-driven end-to-end evals for the NL2DSL pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml
from fastapi.testclient import TestClient

from matcher.matcher_service import ResolvedResult
from service.llm_extractions import Extraction, ExtractionsJson
from service.session_models import QueryState


def _load_cases() -> list[dict]:
    path = Path(__file__).parent / "evals" / "nl2dsl_cases.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("cases", [])


def _make_extraction(payload: dict | None) -> ExtractionsJson:
    payload = payload or {}

    def _items(key: str) -> list[Extraction]:
        return [Extraction(text=text) for text in payload.get(key, [])]

    return ExtractionsJson(
        metric_extractions=_items("metric_extractions"),
        time_extractions=_items("time_extractions"),
        event_extractions=_items("event_extractions"),
        group_by_extractions=_items("group_by_extractions"),
        chart_type_extractions=_items("chart_type_extractions"),
        interaction_mode_extractions=_items("interaction_mode_extractions"),
        region_filter=payload.get("region_filter", []),
    )


def _build_resolver(name: str):
    svc = mock.MagicMock()

    if name == "high_confidence":
        svc.resolve_with_candidates.side_effect = lambda mtype, extractions, default: ResolvedResult(
            value="app_launch" if mtype.value == "event" else ("pv" if mtype.value == "metric" else "country"),
            score=95.0,
            method="exact",
            candidates=[],
            needs_confirmation=False,
        )
        svc.resolve_time.return_value = (7, {"method": "default", "n": 7})
        return svc

    if name == "time_yesterday":
        svc.resolve_time.return_value = (1, {"method": "matched", "n": 1})
        return svc

    if name == "metric_uv":
        def _resolve_metric(mtype, extractions, default):
            if mtype.value == "metric":
                return ResolvedResult(
                    value="uv",
                    score=96.0,
                    method="exact",
                    candidates=[],
                    needs_confirmation=False,
                )
            raise AssertionError(f"unexpected matcher type: {mtype}")

        svc.resolve_with_candidates.side_effect = _resolve_metric
        return svc

    if name == "low_confidence_event":
        def _resolve_event(mtype, extractions, default):
            if mtype.value == "event":
                return ResolvedResult(
                    value="purchase_success",
                    score=55.0,
                    method="fuzzy_low_confidence",
                    candidates=[
                        {"value": "purchase_success", "score": 55.0},
                        {"value": "payment_submit", "score": 48.0},
                    ],
                    needs_confirmation=True,
                )
            return ResolvedResult(
                value="pv" if mtype.value == "metric" else "country",
                score=95.0,
                method="exact",
                candidates=[],
                needs_confirmation=False,
            )

        svc.resolve_with_candidates.side_effect = _resolve_event
        svc.resolve_time.return_value = (7, {"method": "default", "n": 7})
        return svc

    if name == "project_memory_alias":
        def _resolve_alias(mtype, extractions, default):
            if mtype.value == "event":
                return ResolvedResult(
                    value="activation_success",
                    score=97.0,
                    method="memory_alias",
                    candidates=[],
                    needs_confirmation=False,
                )
            return ResolvedResult(
                value="pv" if mtype.value == "metric" else "country",
                score=95.0,
                method="exact",
                candidates=[],
                needs_confirmation=False,
            )

        svc.resolve_with_candidates.side_effect = _resolve_alias
        svc.resolve_time.return_value = (7, {"method": "default", "n": 7})
        return svc

    raise ValueError(f"unknown resolver scenario: {name}")


@pytest.fixture
def eval_client(tmp_path):
    import app as app_module
    from memory.storage.memory_file import TaskStorage
    from memory.user_preference_store import UserPreferenceStore
    from service.session_manager import SessionManager
    from service.task_manager import TaskManager

    app_module.session_manager = SessionManager(data_path=str(tmp_path / "data"))
    app_module.task_manager = TaskManager(
        storage=TaskStorage(data_path=str(tmp_path / "data" / "tasks"))
    )
    app_module.user_preference_store = UserPreferenceStore(
        data_path=str(tmp_path / "data" / "user_preferences")
    )

    # 同步更新 orchestrator 的引用
    app_module.orchestrator.session = app_module.session_manager
    app_module.orchestrator.task = app_module.task_manager
    app_module.orchestrator.preferences = app_module.user_preference_store

    mock_service = mock.MagicMock()
    mock_service.catalog = mock.MagicMock()
    app_module._matcher_service = mock_service

    with TestClient(app_module.app) as c:
        yield c

    app_module._matcher_service = None


def _build_runtime(data_root: Path) -> None:
    import app as app_module
    from memory.storage.memory_file import TaskStorage
    from memory.user_preference_store import UserPreferenceStore
    from service.session_manager import SessionManager
    from service.task_manager import TaskManager

    app_module.session_manager = SessionManager(data_path=str(data_root))
    app_module.task_manager = TaskManager(
        storage=TaskStorage(data_path=str(data_root / "tasks"))
    )
    app_module.user_preference_store = UserPreferenceStore(
        data_path=str(data_root / "user_preferences")
    )

    # 同步更新 orchestrator 的引用
    app_module.orchestrator.session = app_module.session_manager
    app_module.orchestrator.task = app_module.task_manager
    app_module.orchestrator.preferences = app_module.user_preference_store
    mock_service = mock.MagicMock()
    mock_service.catalog = mock.MagicMock()
    app_module._matcher_service = mock_service


def _write_project_memory(case: dict, data_root: Path) -> None:
    setup = case.get("setup") or {}
    project_memory = setup.get("project_memory")
    if not project_memory:
        return

    project_id = project_memory["project_id"]
    project_dir = data_root / "memory" / f"project_{project_id}"
    project_dir.mkdir(parents=True, exist_ok=True)

    links = []
    for entry in project_memory.get("entries", []):
        filename = entry["filename"]
        (project_dir / filename).write_text(entry["content"], encoding="utf-8")
        links.append(f"- [{filename}]({filename})")

    (project_dir / "MEMORY.md").write_text("\n".join(links) + "\n", encoding="utf-8")


def _seed_setup(case: dict) -> str | None:
    import app as app_module

    setup = case.get("setup") or {}
    data_root = Path(app_module.session_manager.storage.data_path).parent
    _write_project_memory(case, data_root)

    last_query_state = setup.get("last_query_state")
    if not last_query_state:
        return None

    ctx = app_module.session_manager.create_or_get(None, "eval_user", last_query_state["project_id"])
    app_module.session_manager.update_query_state(ctx.session_id, QueryState(**last_query_state))
    return ctx.session_id


def _assert_expectations(body: dict, expect: dict) -> None:
    assert body["status"] == expect["status"]

    if "turn_mode" in expect:
        assert body["explain"]["turn_explain"]["mode"] == expect["turn_mode"]

    if "candidates_contains" in expect:
        assert expect["candidates_contains"] in (body.get("candidates") or {})

    semantic_expect = expect.get("semantic")
    if not semantic_expect:
        return

    semantic = body["semantic"]
    if "event" in semantic_expect:
        assert semantic["event"]["event_name"] == semantic_expect["event"]
    if "metric" in semantic_expect:
        assert semantic["metric"]["metric_id"] == semantic_expect["metric"]
    if "region_filter" in semantic_expect:
        assert semantic["region_filter"] == semantic_expect["region_filter"]
    if "group_by" in semantic_expect:
        assert [item["dimension_id"] for item in semantic["group_by"]] == semantic_expect["group_by"]
    if "time_n" in semantic_expect:
        assert semantic["time_range"]["n"] == semantic_expect["time_n"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["name"])
def test_nl2dsl_end_to_end_eval_cases(eval_client, case):
    import app as app_module

    data_root = Path(app_module.session_manager.storage.data_path).parent
    session_id = _seed_setup(case)

    for step in case["steps"]:
        if step.get("restart_before"):
            _build_runtime(data_root)

        request_body = {
            "text": step["text"],
            "project_id": step.get("project_id", 55),
        }
        if session_id:
            request_body["session_id"] = session_id

        patches = [
            mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}),
            mock.patch("service.query_orchestrator.validate_region"),
            mock.patch("service.query_orchestrator.validate_region_consistency"),
        ]

        extraction_payload = step.get("extraction")
        if extraction_payload is not None:
            expected_memory = step.get("assert_memory_contains")

            async def _mock_extract(query, session_context=None, *, _payload=extraction_payload, _expected_memory=expected_memory):
                if _expected_memory:
                    memory_context = (session_context or {}).get("memory_corrections", "")
                    assert _expected_memory in memory_context
                return _make_extraction(_payload)

            patches.append(
                mock.patch(
                    "service.query_orchestrator.extract_llm_async",
                    new_callable=mock.AsyncMock,
                    side_effect=_mock_extract,
                )
            )

        resolver_name = step.get("resolver")
        if resolver_name:
            patches.append(
                mock.patch("app.get_matcher_service", return_value=_build_resolver(resolver_name))
            )

        with patches[0], patches[1], patches[2]:
            extra_contexts = patches[3:]
            if extra_contexts:
                with extra_contexts[0]:
                    if len(extra_contexts) > 1:
                        with extra_contexts[1]:
                            resp = eval_client.post("/nl2dsl", json=request_body)
                    else:
                        resp = eval_client.post("/nl2dsl", json=request_body)
            else:
                resp = eval_client.post("/nl2dsl", json=request_body)

        body = resp.json()
        _assert_expectations(body, step["expect"])
        session_id = body.get("session_id", session_id)

        # eval 期间确保 session 确实持续复用，避免 accidentally stateless
        if session_id:
            assert app_module.session_manager.get_session(session_id) is not None
