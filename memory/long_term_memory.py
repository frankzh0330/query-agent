"""长期记忆管理器

读取 MEMORY.md 索引文件中引用的记忆文件，拼接后用于注入 system prompt。
按 project_id 分桶，避免跨项目污染。

存储结构:
  data/memory/
    ├── _global/                  # 所有项目共享
    │   ├── MEMORY.md
    │   └── common_corrections.md
    ├── project_55/               # 只给 project_id=55 注入
    │   ├── MEMORY.md
    │   └── eu_metrics.md
    └── project_60/               # 只给 project_id=60 注入
        ├── MEMORY.md
        └── us_events.md
"""

import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_MEMORY_LINES = 50
_MAX_MEMORY_BYTES = 5000


class LongTermMemory:
    """长期记忆 — 按 project_id 分桶读取记忆文件并构建注入内容"""

    def __init__(self, data_path: str = "data/memory"):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        # 缓存: key=(project_id,), value=拼接后的内容
        self._cache: Dict[Tuple[int, ...], str] = {}
        self._cache_mtime: Dict[Tuple[int, ...], float] = {}

    def load_memory_context(self, project_id: Optional[int] = None) -> str:
        """加载记忆内容（用于注入 system prompt）

        按 project_id 分桶加载：
        1. 始终加载 _global/ 下的全局记忆
        2. 如果指定了 project_id，额外加载 project_{id}/ 下的项目记忆

        Args:
            project_id: 项目ID，为 None 时只加载全局记忆
        """
        cache_key = (project_id,) if project_id is not None else (None,)

        # 检查缓存
        if cache_key in self._cache and not self._is_cache_stale(cache_key):
            return self._cache[cache_key]

        parts = []

        # 1. 加载全局记忆
        global_dir = self.data_path / "_global"
        global_parts = self._load_dir(global_dir)
        if global_parts:
            parts.extend(global_parts)

        # 2. 加载项目记忆
        if project_id is not None:
            project_dir = self.data_path / f"project_{project_id}"
            project_parts = self._load_dir(project_dir)
            if project_parts:
                parts.extend(project_parts)

        if not parts:
            return ""

        result = "\n\n".join(parts)
        result = self._truncate(result)

        self._cache[cache_key] = result
        self._cache_mtime[cache_key] = self._latest_mtime(project_id)
        return result

    def _load_dir(self, directory: Path) -> list[str]:
        """读取某个目录下的 MEMORY.md 索引及其引用的文件"""
        memory_index = directory / "MEMORY.md"
        if not memory_index.exists():
            return []

        return self._load_all_files(memory_index)

    def _load_all_files(self, memory_index: Path) -> list[str]:
        """读取 MEMORY.md 索引中引用的所有文件内容"""
        try:
            index_content = memory_index.read_text(encoding="utf-8").strip()
        except OSError:
            return []

        base_dir = memory_index.parent
        parts = []
        for line in index_content.split("\n"):
            match = re.match(r"- \[.+?\]\((.+?)\)", line)
            if match:
                file_path = base_dir / match.group(1)
                if file_path.exists():
                    try:
                        content = file_path.read_text(encoding="utf-8")
                        # 去掉 frontmatter（--- ... ---）
                        content = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
                        stripped = content.strip()
                        if stripped:
                            parts.append(stripped)
                    except OSError as e:
                        logger.warning(f"Failed to read memory file {file_path}: {e}")

        return parts

    def _is_cache_stale(self, cache_key: Tuple[int, ...]) -> bool:
        """检查相关目录的文件是否有更新"""
        project_id = cache_key[0]
        current_mtime = self._latest_mtime(project_id)
        cached_mtime = self._cache_mtime.get(cache_key, 0.0)
        return current_mtime > cached_mtime

    def _latest_mtime(self, project_id: Optional[int] = None) -> float:
        """获取相关目录下所有 .md 文件的最新修改时间"""
        latest = 0.0
        dirs = [self.data_path / "_global"]
        if project_id is not None:
            dirs.append(self.data_path / f"project_{project_id}")

        for d in dirs:
            try:
                for f in d.glob("*.md"):
                    latest = max(latest, f.stat().st_mtime)
            except OSError:
                pass
        return latest

    @staticmethod
    def _truncate(
        text: str,
        max_lines: int = _MAX_MEMORY_LINES,
        max_bytes: int = _MAX_MEMORY_BYTES,
    ) -> str:
        """截断保护，避免注入内容占用过多 token"""
        lines = text.split("\n")
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines])
        if len(text.encode("utf-8")) > max_bytes:
            cut = text.rfind("\n", 0, max_bytes)
            text = text[:cut] if cut > 0 else text[:max_bytes]
        return text
