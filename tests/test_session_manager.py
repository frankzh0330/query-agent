"""SessionManager 集成测试"""
import json
from datetime import datetime, timedelta


class TestSessionManager:
    """service/session_manager.py"""

    def test_create_or_get_new_session(self, session_manager):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        assert ctx.session_id
        assert ctx.user_id == "user_1"
        assert ctx.project_id == 55

    def test_create_or_get_returns_existing(self, session_manager):
        ctx1 = session_manager.create_or_get(None, "user_1", 55)
        ctx2 = session_manager.create_or_get(ctx1.session_id, "user_1", 55)
        assert ctx1 is ctx2

    def test_create_or_get_restores_from_jsonl(self, session_manager):
        sid = "restore_test_123"
        # 通过 storage 写入 JSONL 记录
        session_manager.storage.append(sid, {
            "type": "message",
            "role": "user",
            "content": "hello from file",
            "timestamp": datetime.now().isoformat(),
            "metadata": {},
        })
        session_manager.storage.append(sid, {
            "type": "query_state",
            "data": {
                "project_id": 55,
                "event": "app_launch",
                "metric": "pv",
                "time_range": {"type": "last_n_days", "n": 7},
                "region_filter": ["ROW"],
                "group_by": ["country"],
                "filters": [],
            },
            "timestamp": datetime.now().isoformat(),
        })

        ctx = session_manager.create_or_get(sid, "user_1", 55)
        assert ctx is not None
        assert len(ctx.messages) == 1
        assert ctx.messages[0].content == "hello from file"
        assert ctx.last_query_state is not None
        assert ctx.last_query_state.event == "app_launch"

    def test_create_or_get_new_when_jsonl_missing(self, session_manager):
        ctx = session_manager.create_or_get("nonexistent_id", "user_1", 55)
        assert ctx is not None
        assert ctx.session_id == "nonexistent_id"
        assert len(ctx.messages) == 0

    def test_add_message_stores_in_memory(self, session_manager):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.add_message(ctx.session_id, "user", "hello")
        assert len(ctx.messages) == 1
        assert ctx.messages[0].content == "hello"
        assert ctx.messages[0].role == "user"

    def test_add_message_persists_to_jsonl(self, session_manager):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.add_message(ctx.session_id, "user", "hello", metadata={"test": True})

        records = session_manager.storage.read_tail(ctx.session_id)
        assert len(records) == 1
        assert records[0]["type"] == "message"
        assert records[0]["content"] == "hello"

    def test_add_message_truncates_memory(self, tmp_path):
        from service.session_manager import SessionManager
        sm = SessionManager(data_path=str(tmp_path / "data"), max_messages=3)
        ctx = sm.create_or_get(None, "user_1", 55)
        for i in range(5):
            sm.add_message(ctx.session_id, "user", f"msg_{i}")

        # 内存截断到 3 条
        assert len(ctx.messages) == 3
        assert ctx.messages[0].content == "msg_2"
        # JSONL 保留全部 5 条
        records = sm.storage.read_tail(ctx.session_id)
        assert len(records) == 5

    def test_add_message_unknown_session_is_noop(self, session_manager):
        session_manager.add_message("nonexistent", "user", "hello")
        # 不崩溃即可

    def test_update_query_state_stores_in_memory(self, session_manager, sample_query_state):
        from service.session_models import QueryState
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.update_query_state(ctx.session_id, sample_query_state)
        assert ctx.last_query_state is not None
        assert ctx.last_query_state.event == "app_launch"

    def test_update_query_state_persists_to_jsonl(self, session_manager, sample_query_state):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.update_query_state(ctx.session_id, sample_query_state)

        records = session_manager.storage.read_tail(ctx.session_id)
        state_records = [r for r in records if r.get("type") == "query_state"]
        assert len(state_records) == 1
        assert state_records[0]["data"]["event"] == "app_launch"

    def test_get_enhanced_context_includes_session(self, session_manager, sample_query_state):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.add_message(ctx.session_id, "user", "查询PV")
        session_manager.update_query_state(ctx.session_id, sample_query_state)

        context = session_manager.get_enhanced_context(ctx.session_id)
        assert "recent_queries" in context
        assert "查询PV" in context["recent_queries"]
        assert "last_query_state" in context
        assert context["last_query_state"]["event"] == "app_launch"

    def test_get_enhanced_context_includes_memory(self, session_manager, tmp_path):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.add_message(ctx.session_id, "user", "hello")

        # 创建 memory 文件
        global_dir = tmp_path / "data" / "memory" / "_global"
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "corrections.md").write_text("Use ROW for default", encoding="utf-8")
        (global_dir / "MEMORY.md").write_text("- [Corrections](corrections.md)\n", encoding="utf-8")

        context = session_manager.get_enhanced_context(ctx.session_id)
        assert "memory_corrections" in context
        assert "Use ROW for default" in context["memory_corrections"]

    def test_get_enhanced_context_no_memory(self, session_manager):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager.add_message(ctx.session_id, "user", "hello")

        context = session_manager.get_enhanced_context(ctx.session_id)
        assert "memory_corrections" not in context

    def test_cleanup_inactive(self, session_manager):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        session_manager._sessions[ctx.session_id].last_active = datetime.now() - timedelta(minutes=61)

        count = session_manager.cleanup_inactive(max_age_minutes=60)
        assert count == 1
        assert session_manager.get_session(ctx.session_id) is None

    def test_cleanup_inactive_keeps_active(self, session_manager):
        ctx = session_manager.create_or_get(None, "user_1", 55)
        count = session_manager.cleanup_inactive(max_age_minutes=60)
        assert count == 0
        assert session_manager.get_session(ctx.session_id) is ctx

    def test_jsonl_recovery_with_corrupted_lines(self, session_manager):
        sid = "corrupt_test"
        # 写一条有效记录和一条损坏记录
        session_manager.storage.append(sid, {
            "type": "message",
            "role": "user",
            "content": "valid message",
            "timestamp": datetime.now().isoformat(),
            "metadata": {},
        })
        # 写入损坏行
        path = session_manager.storage._session_path(sid)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{{{broken\n")

        ctx = session_manager.create_or_get(sid, "user_1", 55)
        assert ctx is not None
        assert len(ctx.messages) == 1
        assert ctx.messages[0].content == "valid message"
