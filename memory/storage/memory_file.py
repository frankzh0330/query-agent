"""JSONL append-only 存储基类与 Session/Task 实现

- 每条记录一行 JSON，追加写入
- fsync 确保落盘
- 读取时跳过损坏行（容错）
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_COMPACTION_THRESHOLD = 500  # 超过此行数触发 compaction
_COMPACTION_KEEP_TAIL = 100   # compaction 后保留最近 N 条


class JsonlStorage:
    """JSONL append-only 存储基类"""

    def __init__(self, data_path: str):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _file_path(self, record_id: str) -> Path:
        return self.data_path / f"{record_id}.jsonl"

    def append(self, record_id: str, record: Dict[str, Any]) -> None:
        """追加一行记录（append-only，fsync 落盘）"""
        path = self._file_path(record_id)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            logger.error("Failed to append record to %s: %s", record_id, e)
            return

        # 延迟 compaction 检查
        self._maybe_compact(record_id)

    def _maybe_compact(self, record_id: str) -> None:
        """行数超过阈值时自动 compaction，保留 tail + 最新 state/meta"""
        path = self._file_path(record_id)
        if not path.exists():
            return

        try:
            line_count = sum(1 for _ in open(path, "r", encoding="utf-8"))
        except OSError:
            return

        if line_count < _COMPACTION_THRESHOLD:
            return

        records = self.read_all(record_id)

        # 保留最近的记录
        kept = records[-_COMPACTION_KEEP_TAIL:]

        # 确保不丢失最新的 query_state 和 session_meta
        for r in reversed(records):
            if r.get("type") in ("query_state", "session_meta") and r not in kept:
                kept.insert(0, r)
            if len(kept) >= _COMPACTION_KEEP_TAIL + 5:
                break

        # 原子写入
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(path)
            logger.info("Compacted %s: %d → %d records", record_id, len(records), len(kept))
        except OSError as e:
            logger.warning("Compaction failed for %s: %s", record_id, e)
            if tmp_path.exists():
                tmp_path.unlink()

    def read_all(self, record_id: str) -> List[Dict[str, Any]]:
        """读取全部记录（跳过损坏行）"""
        path = self._file_path(record_id)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        results = []
        for line in text.strip().split("\n") if text.strip() else []:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"Skipping corrupted line in {record_id}")
        return results

    def delete(self, record_id: str) -> None:
        """删除文件"""
        path = self._file_path(record_id)
        if path.exists():
            path.unlink()

    def list_ids(self) -> List[str]:
        """列出所有记录 ID"""
        return [p.stem for p in self.data_path.glob("*.jsonl")]


class SessionStorage(JsonlStorage):
    """JSONL append-only session 存储"""

    def __init__(self, data_path: str = "data/sessions"):
        super().__init__(data_path)

    def _session_path(self, session_id: str) -> Path:
        """向后兼容别名"""
        return self._file_path(session_id)

    def read_tail(self, session_id: str, n: int = 20) -> List[Dict[str, Any]]:
        """读取最后 n 条记录"""
        records = self.read_all(session_id)
        return records[-n:] if len(records) > n else records

    def read_last_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """读取最后一条 type=query_state 的记录"""
        records = self.read_tail(session_id, n=50)
        for r in reversed(records):
            if r.get("type") == "query_state":
                return r.get("data")
        return None

    def list_sessions(self) -> List[str]:
        """列出所有 session ID"""
        return self.list_ids()


class TaskStorage(JsonlStorage):
    """JSONL append-only task 存储"""

    def __init__(self, data_path: str = "data/tasks"):
        super().__init__(data_path)

    def read_latest(self, task_id: str) -> Optional[Dict[str, Any]]:
        """读取最新一条记录"""
        records = self.read_all(task_id)
        return records[-1] if records else None

    def list_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        """列出指定 session 的所有 task"""
        results: List[Dict[str, Any]] = []
        for path in self.data_path.glob("*.jsonl"):
            latest = self.read_latest(path.stem)
            if latest and latest.get("session_id") == session_id:
                results.append(latest)
        return results
