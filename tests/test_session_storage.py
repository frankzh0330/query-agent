"""SessionStorage JSONL 持久化测试"""
import json


class TestSessionStorage:
    """memory/storage/memory_file.py"""

    def test_append_creates_file(self, session_storage):
        session_storage.append("sess_1", {"type": "message", "text": "hello"})
        path = session_storage._session_path("sess_1")
        assert path.exists()

    def test_append_writes_valid_jsonl(self, session_storage):
        records = [
            {"type": "message", "text": "first"},
            {"type": "message", "text": "second"},
            {"type": "message", "text": "third"},
        ]
        for r in records:
            session_storage.append("sess_1", r)

        path = session_storage._session_path("sess_1")
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        for i, line in enumerate(lines):
            parsed = json.loads(line)
            assert parsed["text"] == records[i]["text"]

    def test_read_tail_returns_last_n(self, session_storage):
        for i in range(10):
            session_storage.append("sess_1", {"type": "message", "idx": i})

        result = session_storage.read_tail("sess_1", n=3)
        assert len(result) == 3
        assert [r["idx"] for r in result] == [7, 8, 9]

    def test_read_tail_nonexistent_session(self, session_storage):
        result = session_storage.read_tail("nonexistent")
        assert result == []

    def test_read_tail_empty_file(self, session_storage):
        path = session_storage._session_path("empty_sess")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

        result = session_storage.read_tail("empty_sess")
        assert result == []

    def test_read_last_state_finds_query_state(self, session_storage):
        session_storage.append("sess_1", {"type": "message", "text": "hello"})
        session_storage.append("sess_1", {
            "type": "query_state",
            "data": {"event": "app_launch", "metric": "pv"},
        })
        session_storage.append("sess_1", {"type": "message", "text": "world"})
        session_storage.append("sess_1", {
            "type": "query_state",
            "data": {"event": "payment", "metric": "uv"},
        })

        result = session_storage.read_last_state("sess_1")
        assert result is not None
        assert result["event"] == "payment"
        assert result["metric"] == "uv"

    def test_read_last_state_no_state_records(self, session_storage):
        session_storage.append("sess_1", {"type": "message", "text": "hello"})
        session_storage.append("sess_1", {"type": "message", "text": "world"})

        result = session_storage.read_last_state("sess_1")
        assert result is None

    def test_read_last_state_nonexistent(self, session_storage):
        result = session_storage.read_last_state("nonexistent")
        assert result is None

    def test_corrupted_line_skipped(self, session_storage):
        path = session_storage._session_path("sess_1")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message", "text": "valid1"}) + "\n")
            f.write("{{{broken json\n")
            f.write(json.dumps({"type": "message", "text": "valid2"}) + "\n")

        result = session_storage.read_tail("sess_1")
        assert len(result) == 2
        assert result[0]["text"] == "valid1"
        assert result[1]["text"] == "valid2"

    def test_all_corrupted_lines(self, session_storage):
        path = session_storage._session_path("sess_1")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("broken1\n")
            f.write("broken2\n")

        result = session_storage.read_tail("sess_1")
        assert result == []

    def test_delete_removes_file(self, session_storage):
        session_storage.append("sess_1", {"type": "message"})
        assert session_storage._session_path("sess_1").exists()

        session_storage.delete("sess_1")
        assert not session_storage._session_path("sess_1").exists()

    def test_delete_nonexistent_is_noop(self, session_storage):
        session_storage.delete("nonexistent")  # should not raise

    def test_list_sessions(self, session_storage):
        for sid in ["sess_a", "sess_b", "sess_c"]:
            session_storage.append(sid, {"type": "message"})

        sessions = session_storage.list_sessions()
        assert set(sessions) == {"sess_a", "sess_b", "sess_c"}

    def test_list_sessions_empty(self, session_storage):
        assert session_storage.list_sessions() == []

    def test_append_multiple_sessions_independent(self, session_storage):
        session_storage.append("sess_a", {"type": "message", "text": "a1"})
        session_storage.append("sess_b", {"type": "message", "text": "b1"})
        session_storage.append("sess_a", {"type": "message", "text": "a2"})

        result_a = session_storage.read_tail("sess_a")
        result_b = session_storage.read_tail("sess_b")
        assert len(result_a) == 2
        assert len(result_b) == 1
        assert result_a[0]["text"] == "a1"
        assert result_b[0]["text"] == "b1"
