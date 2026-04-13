"""JSONL append-only Session 存储

替代旧版 Markdown 存储，提供可靠的 session 持久化：
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


class SessionStorage:
    """JSONL append-only session 存储"""

    def __init__(self, data_path: str = "data/sessions"):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.data_path / f"{session_id}.jsonl"

    def append(self, session_id: str, record: Dict[str, Any]) -> None:
        """追加一行记录（append-only，fsync 落盘）"""
        path = self._session_path(session_id)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            logger.error(f"Failed to append session record: {e}")

    def read_tail(self, session_id: str, n: int = 20) -> List[Dict[str, Any]]:
        """读取最后 n 条记录"""
        path = self._session_path(session_id)
        if not path.exists():
            return []

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []

        lines = text.strip().split("\n") if text.strip() else []
        results = []
        for line in lines[-n:]:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"Skipping corrupted line in session {session_id}")
                continue
        return results

    def read_last_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """读取最后一条 type=query_state 的记录"""
        # 只读最后 50 条，避免大文件全量扫描
        records = self.read_tail(session_id, n=50)
        for r in reversed(records):
            if r.get("type") == "query_state":
                return r.get("data")
        return None

    def delete(self, session_id: str) -> None:
        """删除 session 文件"""
        path = self._session_path(session_id)
        if path.exists():
            path.unlink()

    def list_sessions(self) -> List[str]:
        """列出所有 session ID"""
        return [p.stem for p in self.data_path.glob("*.jsonl")]
