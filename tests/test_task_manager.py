"""TaskManager 确认流生命周期测试"""
from datetime import datetime, timedelta


class TestTaskManager:
    """service/task_manager.py"""

    def test_create_task_returns_task_context(self, task_manager):
        task = task_manager.create_task(
            session_id="sess_1",
            raw_query="purchase的情况",
            extraction={"tables": ["orders"]},
            user_id="user_1",
            candidates={"table": [{"value": "payment_submit", "score": 0.88}]},
        )
        assert task.task_id
        assert task.status == "waiting_confirmation"
        assert task.session_id == "sess_1"
        assert task.raw_query == "purchase的情况"

    def test_create_task_generates_unique_ids(self, task_manager):
        t1 = task_manager.create_task("s1", "q1", {})
        t2 = task_manager.create_task("s1", "q2", {})
        assert t1.task_id != t2.task_id

    def test_get_task_returns_task(self, task_manager):
        created = task_manager.create_task("s1", "q1", {})
        found = task_manager.get_task(created.task_id)
        assert found is created

    def test_get_task_nonexistent_returns_none(self, task_manager):
        assert task_manager.get_task("nonexistent") is None

    def test_get_task_expired_returns_none(self, task_manager):
        task = task_manager.create_task("s1", "q1", {})
        # 手动设置 created_at 为 31 分钟前
        task_manager._tasks[task.task_id].created_at = datetime.now() - timedelta(minutes=31)
        assert task_manager.get_task(task.task_id) is None

    def test_get_task_expired_marks_status(self, task_manager):
        task = task_manager.create_task("s1", "q1", {})
        task_manager._tasks[task.task_id].created_at = datetime.now() - timedelta(minutes=31)
        task_manager.get_task(task.task_id)
        assert task.status == "expired"

    def test_confirm_task_records_selection(self, task_manager):
        task = task_manager.create_task(
            "s1", "q1", {},
            candidates={"table": [{"value": "orders", "score": 0.55}]},
            partial_query_state=QueryState(project_id=55),
        )
        result = task_manager.confirm_task(task.task_id, "table", "orders")
        assert result.user_selection["table"] == "orders"

    def test_confirm_task_patches_query_state(self, task_manager):
        from service.session_models import QueryState
        qs = QueryState(project_id=55)
        task_manager.create_task(
            "s1", "q1", {},
            candidates={"table": [{"value": "x", "score": 0.5}]},
            partial_query_state=qs,
        )
        task_id = task_manager.list_tasks()[0].task_id
        task_manager.confirm_task(task_id, "tables", "orders")
        assert qs.tables == ["orders"]

    def test_confirm_task_idempotent(self, task_manager):
        task = task_manager.create_task(
            "s1", "q1", {},
            candidates={"table": [{"value": "x", "score": 0.5}]},
        )
        task_manager.confirm_task(task.task_id, "table", "a")
        task_manager.confirm_task(task.task_id, "table", "b")
        assert task.user_selection["table"] == "b"

    def test_confirm_task_partial_confirm(self, task_manager):
        task = task_manager.create_task(
            "s1", "q1", {},
            candidates={
                "table": [{"value": "x", "score": 0.5}],
                "metric": [{"value": "revenue", "score": 0.6}],
            },
        )
        task_manager.confirm_task(task.task_id, "table", "x")
        assert task.status == "waiting_confirmation"

    def test_confirm_task_all_confirmed(self, task_manager):
        task = task_manager.create_task(
            "s1", "q1", {},
            candidates={
                "table": [{"value": "x", "score": 0.5}],
                "metric": [{"value": "revenue", "score": 0.6}],
            },
        )
        task_manager.confirm_task(task.task_id, "table", "x")
        task_manager.confirm_task(task.task_id, "metric", "revenue")
        assert task.status == "confirmed"

    def test_confirm_task_expired_returns_none(self, task_manager):
        task = task_manager.create_task("s1", "q1", {}, candidates={"table": []})
        task_manager._tasks[task.task_id].created_at = datetime.now() - timedelta(minutes=31)
        result = task_manager.confirm_task(task.task_id, "table", "x")
        assert result is None

    def test_update_status_valid_transition(self, task_manager):
        task = task_manager.create_task("s1", "q1", {})
        r1 = task_manager.update_status(task.task_id, "confirmed")
        assert r1.status == "confirmed"
        r2 = task_manager.update_status(task.task_id, "completed")
        assert r2.status == "completed"

    def test_update_status_invalid_transition(self, task_manager):
        task = task_manager.create_task("s1", "q1", {})
        result = task_manager.update_status(task.task_id, "completed")
        # waiting_confirmation → completed is invalid
        assert result.status == "waiting_confirmation"

    def test_update_status_cancelled_from_waiting(self, task_manager):
        task = task_manager.create_task("s1", "q1", {})
        result = task_manager.update_status(task.task_id, "cancelled")
        assert result.status == "cancelled"

    def test_expire_task(self, task_manager):
        task = task_manager.create_task("s1", "q1", {})
        task_manager.expire_task(task.task_id)
        assert task.status == "expired"

    def test_cleanup_expired(self, task_manager):
        fresh = task_manager.create_task("s1", "q1", {})
        old = task_manager.create_task("s1", "q2", {})
        task_manager._tasks[old.task_id].created_at = datetime.now() - timedelta(minutes=31)

        count = task_manager.cleanup_expired()
        assert count == 1
        assert task_manager.get_task(fresh.task_id) is fresh

    def test_list_tasks_all(self, task_manager):
        task_manager.create_task("s1", "q1", {})
        task_manager.create_task("s2", "q2", {})
        assert len(task_manager.list_tasks()) == 2

    def test_list_tasks_filtered_by_session(self, task_manager):
        task_manager.create_task("s1", "q1", {})
        task_manager.create_task("s2", "q2", {})
        assert len(task_manager.list_tasks(session_id="s1")) == 1

    def test_update_status_nonexistent_returns_none(self, task_manager):
        assert task_manager.update_status("nonexistent", "confirmed") is None

    def test_create_task_persists_to_storage(self, task_manager):
        task = task_manager.create_task("s1", "q1", {}, candidates={"table": [{"value": "x", "score": 0.5}]})
        latest = task_manager.storage.read_latest(task.task_id)
        assert latest is not None
        assert latest["status"] == "waiting_confirmation"
        assert latest["session_id"] == "s1"

    def test_get_task_restores_from_storage(self, task_manager):
        task = task_manager.create_task("s1", "q1", {}, candidates={"table": [{"value": "x", "score": 0.5}]})
        task_manager._tasks.clear()
        restored = task_manager.get_task(task.task_id)
        assert restored is not None
        assert restored.task_id == task.task_id
        assert restored.session_id == "s1"

    def test_confirm_task_persists_updated_state(self, task_manager):
        task = task_manager.create_task(
            "s1", "q1", {},
            candidates={"table": [{"value": "orders", "score": 0.55}]},
        )
        task_manager.confirm_task(task.task_id, "table", "orders")
        latest = task_manager.storage.read_latest(task.task_id)
        assert latest["user_selection"]["table"] == "orders"
        assert latest["status"] == "confirmed"


# 避免顶层 import 冲突，在测试方法内引用
from service.session_models import QueryState
