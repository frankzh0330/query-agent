"""长期记忆管理器

读取 MEMORY.md 索引文件中引用的记忆文件，拼接后用于注入 system prompt。

借鉴 cc_python 的 load_memory_prompt() 设计，但简化为：
- 不注入"如何管理记忆"的 instruction（LLM 是提取器，不管理记忆）
- 记忆文件由服务端/人工管理
- 截断保护避免 token 溢出
- 文件 mtime 检测实现缓存失效
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_MEMORY_LINES = 50
_MAX_MEMORY_BYTES = 5000


class LongTermMemory:
    """长期记忆 — 读取记忆文件并构建注入内容

    存储结构:
      data/memory/
        ├── MEMORY.md                 # 索引文件，格式: - [Title](file.md) — 描述
        ├── user_corrections.md       # 纠正记忆（frontmatter + 内容）
        └── domain_constraints.md     # 领域约束（frontmatter + 内容）
    """

    def __init__(self, data_path: str = "data/memory"):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[str] = None
        self._cache_mtime: float = 0

    def load_memory_context(self) -> str:
        """加载所有记忆内容（用于注入 system prompt）

        流程:
        1. 检查缓存是否过期
        2. 读取 MEMORY.md 索引
        3. 读取各记忆文件，去掉 frontmatter
        4. 拼接 + 截断保护
        """
        if self._cache and not self._is_cache_stale():
            return self._cache

        memory_index = self.data_path / "MEMORY.md"
        if not memory_index.exists():
            return ""

        parts = self._load_all_files(memory_index)

        if not parts:
            return ""

        result = "\n\n".join(parts)
        result = self._truncate(result)

        self._cache = result
        self._cache_mtime = self._latest_mtime()
        return result

    def _load_all_files(self, memory_index: Path) -> list[str]:
        """读取 MEMORY.md 索引中引用的所有文件内容"""
        try:
            index_content = memory_index.read_text(encoding="utf-8").strip()
        except OSError:
            return []

        parts = []
        for line in index_content.split("\n"):
            match = re.match(r"- \[.+?\]\((.+?)\)", line)
            if match:
                file_path = self.data_path / match.group(1)
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

    def _is_cache_stale(self) -> bool:
        """检查记忆文件是否有更新"""
        return self._latest_mtime() > self._cache_mtime

    def _latest_mtime(self) -> float:
        """获取 memory 目录下所有 .md 文件的最新修改时间"""
        latest = 0.0
        try:
            for f in self.data_path.glob("*.md"):
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
