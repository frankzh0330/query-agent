"""app.py HTTP 端点测试（替代 test_nl2dsl_api.py）"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from matcher.matcher_service import ResolvedResult
from service.llm_extractions import Extraction, ExtractionsJson
from service.session_models import QueryState


# ==================== Fixtures ====================

@pytest.fixture
def client(tmp_path):
    """创建隔离的 TestClient，使用 tmp_path 避免 state 泄漏"""
    import app as app_module
    from memory.storage.memory_file import TaskStorage
    from memory.user_preference_store import UserPreferenceStore
    from service.session_manager import SessionManager
    from service.task_manager import TaskManager

    # 替换全局实例为隔离版本
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

    # 初始化 matcher_service（用 mock 避免加载真实 catalog）
    mock_service = mock.MagicMock()
    mock_service.catalog = mock.MagicMock()
    app_module._matcher_service = mock_service

    with TestClient(app_module.app) as c:
        yield c

    # 清理
    app_module._matcher_service = None


def _mock_high_confidence_resolver():
    """高置信度 resolver（不需要确认）"""
    svc = mock.MagicMock()
    svc.resolve_with_candidates.side_effect = lambda mtype, extractions, default: ResolvedResult(
        value="app_launch" if mtype.value == "event" else ("pv" if mtype.value == "metric" else "country"),
        score=95.0,
        method="exact",
        candidates=[],
        needs_confirmation=False,
    )
    svc.resolve_time.return_value = (7, {"method": "default", "n": 7})
    return svc


def _mock_low_confidence_resolver():
    """低置信度 resolver（需要确认）"""
    svc = mock.MagicMock()
    call_count = {"n": 0}

    def _resolve(mtype, extractions, default):
        call_count["n"] += 1
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

    svc.resolve_with_candidates.side_effect = _resolve
    svc.resolve_time.return_value = (7, {"method": "default", "n": 7})
    return svc


def _mock_low_score_resolver():
    """极低分 resolver（fallback 到 default）"""
    svc = mock.MagicMock()
    svc.resolve_with_candidates.side_effect = lambda mtype, extractions, default: ResolvedResult(
        value=default,
        score=20.0,
        method="score_too_low",
        candidates=[],
        needs_confirmation=False,
    )
    svc.resolve_time.return_value = (7, {"method": "default", "n": 7})
    return svc


# ==================== Tests: Happy Path ====================

class TestNL2DSLHappyPath:
    @staticmethod
    def _seed_previous_state():
        import app as app_module

        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)
        prev_qs = QueryState(
            project_id=55,
            event="app_launch",
            metric="pv",
            time_range={"type": "last_n_days", "n": 7},
            region_filter=["ROW"],
            group_by=["country"],
            filters=[],
            turn_type="new_query",
        )
        app_module.session_manager.update_query_state(ctx.session_id, prev_qs)
        return ctx, prev_qs

    def test_nl2dsl_success(self, client):
        extraction = ExtractionsJson(
            metric_extractions=[Extraction(text="PV")],
            event_extractions=[Extraction(text="app_launch")],
            region_filter=["ROW"],
            group_by_extractions=[Extraction(text="country")],
        )
        mock_service = _mock_high_confidence_resolver()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "查询近7天 app_launch 的PV",
                            "project_id": 55,
                        })

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "semantic" in body
        assert "exec_dsl" in body
        assert body["session_id"]

    def test_nl2dsl_returns_session_id(self, client):
        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="app_launch")],
            region_filter=["ROW"],
        )
        mock_service = _mock_high_confidence_resolver()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={"text": "test", "project_id": 55})

        assert resp.json()["session_id"] is not None

    def test_nl2dsl_uses_existing_session(self, client):
        import app as app_module
        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)
        sid = ctx.session_id

        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="app_launch")],
            region_filter=["ROW"],
        )
        mock_service = _mock_high_confidence_resolver()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "test",
                            "project_id": 55,
                            "session_id": sid,
                        })

        assert resp.json()["session_id"] == sid

    def test_nl2dsl_followup_inherits_previous_state(self, client):
        ctx, _ = self._seed_previous_state()

        extraction = ExtractionsJson(
            time_extractions=[Extraction(text="昨天")],
            event_extractions=[],
            region_filter=["ROW"],
        )
        mock_service = mock.MagicMock()
        mock_service.resolve_time.return_value = (1, {"method": "matched", "n": 1})

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "昨天",
                            "project_id": 55,
                            "session_id": ctx.session_id,
                        })

        body = resp.json()
        assert body["status"] == "success"
        assert body["semantic"]["event"]["event_name"] == "app_launch"
        assert body["semantic"]["metric"]["metric_id"] == "pv"
        assert body["semantic"]["time_range"]["n"] == 1
        assert body["explain"]["turn_explain"]["mode"] == "followup_patch"
        assert body["explain"]["turn_explain"]["decision"]["reason"] == "time_only_term"
        assert body["explain"]["turn_explain"]["applied_patch_fields"] == ["time_range"]

    def test_nl2dsl_followup_changes_metric_only(self, client):
        ctx, _ = self._seed_previous_state()

        extraction = ExtractionsJson(
            metric_extractions=[Extraction(text="UV")],
            event_extractions=[],
            region_filter=["ROW"],
        )
        mock_service = mock.MagicMock()

        def _resolve(mtype, extractions, default):
            if mtype.value == "metric":
                return ResolvedResult(
                    value="uv",
                    score=96.0,
                    method="exact",
                    candidates=[],
                    needs_confirmation=False,
                )
            raise AssertionError(f"unexpected matcher type: {mtype}")

        mock_service.resolve_with_candidates.side_effect = _resolve

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "改成UV",
                            "project_id": 55,
                            "session_id": ctx.session_id,
                        })

        body = resp.json()
        assert body["status"] == "success"
        assert body["semantic"]["metric"]["metric_id"] == "uv"
        assert body["semantic"]["event"]["event_name"] == "app_launch"
        assert body["explain"]["turn_explain"]["explicit_fields"] == ["metric"]
        assert body["explain"]["turn_explain"]["decision"]["matched_signals"]
        assert body["explain"]["turn_explain"]["state_snapshot"]["metric"] == "uv"

    def test_nl2dsl_followup_changes_region_only(self, client):
        ctx, _ = self._seed_previous_state()

        extraction = ExtractionsJson(
            event_extractions=[],
            region_filter=["USTTP"],
        )
        mock_service = mock.MagicMock()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "那美国呢",
                            "project_id": 55,
                            "session_id": ctx.session_id,
                        })

        body = resp.json()
        assert body["status"] == "success"
        assert body["semantic"]["region_filter"] == ["USTTP"]
        assert body["semantic"]["metric"]["metric_id"] == "pv"
        assert body["semantic"]["event"]["event_name"] == "app_launch"
        assert body["explain"]["turn_explain"]["explicit_fields"] == ["region_filter"]
        assert "region_token" in body["explain"]["turn_explain"]["decision"]["matched_signals"]

    def test_nl2dsl_followup_changes_group_by_only(self, client):
        ctx, _ = self._seed_previous_state()

        extraction = ExtractionsJson(
            event_extractions=[],
            region_filter=["ROW"],
            group_by_extractions=[Extraction(text="渠道")],
        )
        mock_service = mock.MagicMock()

        def _resolve(mtype, extractions, default):
            if mtype.value == "dimension":
                return ResolvedResult(
                    value="channel",
                    score=94.0,
                    method="exact",
                    candidates=[],
                    needs_confirmation=False,
                )
            raise AssertionError(f"unexpected matcher type: {mtype}")

        mock_service.resolve_with_candidates.side_effect = _resolve

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "再按渠道拆一下",
                            "project_id": 55,
                            "session_id": ctx.session_id,
                        })

        body = resp.json()
        assert body["status"] == "success"
        assert body["semantic"]["group_by"] == [{"dimension_id": "channel"}]
        assert body["semantic"]["metric"]["metric_id"] == "pv"
        assert body["semantic"]["event"]["event_name"] == "app_launch"
        assert body["explain"]["turn_explain"]["explicit_fields"] == ["group_by"]
        assert body["explain"]["turn_explain"]["applied_patch"]["group_by"] == ["channel"]

    def test_nl2dsl_applies_user_preference_rerank_after_recall(self, client):
        import app as app_module

        for _ in range(6):
            app_module.user_preference_store.record_selection(
                55, "user_pref", event="payment_submit"
            )

        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="支付")],
            region_filter=["ROW"],
        )
        mock_service = mock.MagicMock()

        def _resolve(mtype, extractions, default):
            if mtype.value == "event":
                return ResolvedResult(
                    value="purchase_success",
                    score=55.0,
                    method="fuzzy_low_confidence",
                    candidates=[
                        {"value": "purchase_success", "score": 55.0},
                        {"value": "payment_submit", "score": 54.0},
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

        mock_service.resolve_with_candidates.side_effect = _resolve
        mock_service.resolve_time.return_value = (7, {"method": "default", "n": 7})

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                resp = client.post("/nl2dsl", json={
                    "text": "支付情况",
                    "project_id": 55,
                    "user_id": "user_pref",
                })

        body = resp.json()
        assert body["status"] == "needs_confirmation"
        assert body["candidates"]["event"][0]["value"] == "payment_submit"
        assert body["explain"]["resolver_explain"]["event"]["user_preference_bias"]["applied"] is True


# ==================== Tests: Early Exit ====================

class TestNL2DSLEarlyExit:

    def test_nl2dsl_early_exit_no_event(self, client):
        extraction = ExtractionsJson(
            metric_extractions=[Extraction(text="PV")],
            event_extractions=[],
            region_filter=["ROW"],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            resp = client.post("/nl2dsl", json={
                "text": "PV数据",
                "project_id": 55,
            })

        body = resp.json()
        assert body["status"] == "early_exit"
        assert body["message"]
        assert "event" in body["message"]

    def test_nl2dsl_early_exit_records_message(self, client):
        import app as app_module

        extraction = ExtractionsJson(
            event_extractions=[],
            region_filter=["ROW"],
        )

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            resp = client.post("/nl2dsl", json={
                "text": "some query",
                "project_id": 55,
            })

        sid = resp.json()["session_id"]
        ctx = app_module.session_manager.get_session(sid)
        assert ctx is not None
        assert any(m.content == "some query" for m in ctx.messages)


# ==================== Tests: Confirmation Flow ====================

class TestNL2DSLConfirmation:
    @staticmethod
    def _seed_previous_state():
        import app as app_module

        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)
        prev_qs = QueryState(
            project_id=55,
            event="app_launch",
            metric="pv",
            time_range={"type": "last_n_days", "n": 7},
            region_filter=["ROW"],
            group_by=["country"],
            filters=[],
            turn_type="new_query",
        )
        app_module.session_manager.update_query_state(ctx.session_id, prev_qs)
        return ctx, prev_qs

    def test_nl2dsl_needs_confirmation(self, client):
        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="purchase")],
            region_filter=["ROW"],
        )
        mock_service = _mock_low_confidence_resolver()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                resp = client.post("/nl2dsl", json={
                    "text": "purchase的情况",
                    "project_id": 55,
                })

        body = resp.json()
        assert body["status"] == "needs_confirmation"
        assert body["task_id"] is not None
        assert body["candidates"] is not None
        assert "event" in body["candidates"]

    def test_nl2dsl_confirmation_then_reply(self, client):
        import app as app_module

        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="purchase")],
            region_filter=["ROW"],
        )
        mock_service = _mock_low_confidence_resolver()

        # 第一次请求：触发确认
        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                resp1 = client.post("/nl2dsl", json={
                    "text": "purchase的情况",
                    "project_id": 55,
                })

        body1 = resp1.json()
        sid = body1["session_id"]
        task_id = body1["task_id"]
        assert body1["status"] == "needs_confirmation"

        # 第二次请求：用户回复确认
        with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
            with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                resp2 = client.post("/nl2dsl", json={
                    "text": "1",
                    "project_id": 55,
                    "session_id": sid,
                })

        body2 = resp2.json()
        assert body2["status"] == "success"
        assert "confirmed" in body2.get("message", "") or body2["status"] == "success"
        assert body2["explain"]["turn_explain"]["mode"] == "confirmation"
        assert body2["explain"]["turn_explain"]["field_sources"]["event"] == "confirmed"
        assert body2["explain"]["turn_explain"]["confirmed_fields"]["event"] == "purchase_success"

    def test_nl2dsl_followup_confirmation_then_reply(self, client):
        ctx, _ = self._seed_previous_state()

        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="purchase")],
            region_filter=["ROW"],
        )
        mock_service = _mock_low_confidence_resolver()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                resp1 = client.post("/nl2dsl", json={
                    "text": "对比purchase",
                    "project_id": 55,
                    "session_id": ctx.session_id,
                })

        body1 = resp1.json()
        assert body1["status"] == "needs_confirmation"
        assert body1["explain"]["turn_explain"]["mode"] == "followup_patch"

        with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
            with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                resp2 = client.post("/nl2dsl", json={
                    "text": "1",
                    "project_id": 55,
                    "session_id": ctx.session_id,
                })

        body2 = resp2.json()
        assert body2["status"] == "success"
        assert body2["semantic"]["event"]["event_name"] == "purchase_success"
        assert body2["semantic"]["metric"]["metric_id"] == "pv"
        assert body2["semantic"]["group_by"] == [{"dimension_id": "country"}]
        assert body2["explain"]["turn_explain"]["mode"] == "confirmation"
        assert body2["explain"]["turn_explain"]["field_sources"]["event"] == "confirmed"
        assert "metric" in body2["explain"]["turn_explain"]["inherited_fields"]
        assert body2["explain"]["turn_explain"]["confirmed_fields"]["event"] == "purchase_success"

    def test_nl2dsl_confirmation_after_session_restore(self, client):
        import app as app_module
        from service.session_manager import SessionManager

        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="purchase")],
            region_filter=["ROW"],
        )
        mock_service = _mock_low_confidence_resolver()

        # 第一次请求：触发确认并落盘
        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                resp1 = client.post("/nl2dsl", json={
                    "text": "purchase的情况",
                    "project_id": 55,
                })

        body1 = resp1.json()
        sid = body1["session_id"]
        assert body1["status"] == "needs_confirmation"

        # 模拟服务重启：清空内存 session 和 task，再从同一路径恢复
        original_session_manager = app_module.session_manager
        original_task_manager = app_module.task_manager
        app_module.session_manager = SessionManager(
            data_path=str(original_session_manager.storage.data_path.parent)
        )
        app_module.task_manager = type(original_task_manager)(
            ttl_minutes=30,
            storage=original_task_manager.storage,
        )

        try:
            with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                    resp2 = client.post("/nl2dsl", json={
                        "text": "1",
                        "project_id": 55,
                        "session_id": sid,
                    })
        finally:
            # 恢复 fixture 中的隔离实例，避免影响后续测试
            app_module.session_manager = original_session_manager
            app_module.task_manager = original_task_manager

        body2 = resp2.json()
        assert body2["status"] == "success"


# ==================== Tests: Fallback ====================

class TestNL2DSLFallback:

    def test_nl2dsl_fallback_on_low_score(self, client):
        extraction = ExtractionsJson(
            event_extractions=[Extraction(text="unknown_thing")],
            region_filter=["ROW"],
        )
        mock_service = _mock_low_score_resolver()

        with mock.patch("service.query_orchestrator.extract_llm_async", new_callable=mock.AsyncMock, return_value=extraction):
            with mock.patch("app.get_matcher_service", return_value=mock_service):
                with mock.patch("service.query_orchestrator.render_exec_dsl", return_value={"content": {"queries": []}}):
                    with mock.patch("service.query_orchestrator.validate_region"), mock.patch("service.query_orchestrator.validate_region_consistency"):
                        resp = client.post("/nl2dsl", json={
                            "text": "unknown query",
                            "project_id": 55,
                        })

        body = resp.json()
        assert body["status"] == "success"
        # 使用了 default 值（app_launch / pv / country）
        assert body["semantic"]["event"]["event_name"] == "app_launch"
        assert body["semantic"]["metric"]["metric_id"] == "pv"


# ==================== Tests: Bearer Query ====================

class TestBearerQuery:

    def test_query_bearer_success(self, client):
        with mock.patch("app.execute_query", new_callable=mock.AsyncMock, return_value={"data": "mock"}):
            resp = client.post("/query/bearer", json={"exec_dsl": {"content": {}}})

        body = resp.json()
        assert body["success"] is True
        assert body["result"] == {"data": "mock"}

    def test_query_bearer_failure(self, client):
        with mock.patch("app.execute_query", new_callable=mock.AsyncMock, side_effect=ValueError("API error")):
            resp = client.post("/query/bearer", json={"exec_dsl": {"content": {}}})

        body = resp.json()
        assert body["success"] is False
        assert "API error" in body["error"]


# ==================== Tests: Session Endpoints ====================

class TestSessionEndpoints:

    def test_list_sessions_empty(self, client):
        resp = client.get("/sessions")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_sessions_with_data(self, client):
        import app as app_module
        app_module.session_manager.create_or_get(None, "user_1", 55)

        resp = client.get("/sessions")
        assert resp.json()["count"] == 1

    def test_get_session_found(self, client):
        import app as app_module
        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)

        resp = client.get(f"/sessions/{ctx.session_id}")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == ctx.session_id

    def test_get_session_not_found(self, client):
        resp = client.get("/sessions/nonexistent")
        assert resp.status_code == 404

    def test_delete_session_found(self, client):
        import app as app_module
        ctx = app_module.session_manager.create_or_get(None, "user_1", 55)

        resp = client.delete(f"/sessions/{ctx.session_id}")
        assert resp.status_code == 200

        resp2 = client.get(f"/sessions/{ctx.session_id}")
        assert resp2.status_code == 404

    def test_delete_session_not_found(self, client):
        resp = client.delete("/sessions/nonexistent")
        assert resp.status_code == 404
