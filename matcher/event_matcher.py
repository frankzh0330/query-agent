from __future__ import annotations

from typing import Any, Dict, Set

import jieba

from matcher.base import DEFAULT_STOPWORDS, DEFAULT_SYNONYM_MAP, BaseMatcher, _stable_id


class EventMatcher(BaseMatcher):
    """
    事件匹配器，继承 BaseMatcher

    支持：
    1. exact alias 命中
    2. 中英混合分词
    3. 倒排索引召回
    4. RapidFuzz 重排
    5. query 侧同义词扩展
    """

    def _build_index(self, catalog_events: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """
        构建事件索引

        :param catalog_events: 结构示例：
            {
                "app_launch": {
                    "aliases": ["应用启动", "启动应用", "app启动"]
                },
                "payment_success": {
                    "aliases": ["支付成功", "支付完成"]
                }
            }
        """
        tmp_index: Dict[str, Set[int]] = {}

        # 先分配 event_id（使用稳定哈希 ID）
        for event_name, info in catalog_events.items():
            event_id = _stable_id(event_name)  # 稳定的整数 ID
            aliases = info.get("aliases", []) or []
            all_aliases = [event_name] + list(aliases)
            all_aliases = list(dict.fromkeys(all_aliases))  # 去重

            self.name_to_id[event_name] = event_id
            self.id_to_doc[event_id] = {
                "event_id": event_id,
                "event_name": event_name,
                "aliases": all_aliases,
            }
            self.id_to_norm_aliases[event_id] = []

            # 加入 jieba 词典
            if enable_jieba_userdict:
                self._add_to_jieba_dict(all_aliases)

        # 再建 exact map + 倒排索引
        for event_id, doc in self.id_to_doc.items():
            self._build_inverted_index(event_id, doc["aliases"], tmp_index)

        self.token_to_ids = self._finalize_index(tmp_index)

    def _get_name_by_id(self, doc_id: int) -> str:
        return self.id_to_doc[doc_id]["event_name"]


# =========================
# 帮助函数：从 catalog 创建 matcher
# =========================

def build_event_matcher_from_catalog(catalog_events: Dict[str, Dict[str, Any]]) -> EventMatcher:
    """从 catalog 创建 EventMatcher"""
    return EventMatcher(
        catalog_data=catalog_events,
        stopwords=DEFAULT_STOPWORDS,
        synonym_map=DEFAULT_SYNONYM_MAP,
        fuzzy_threshold=70.0,
        max_candidates=200,
        max_posting_per_token=1000,
        enable_jieba_userdict=True,
    )


# =========================
# 本地测试
# =========================

if __name__ == "__main__":
    mock_catalog_events = {
        "app_launch": {
            "aliases": [
                "应用启动",
                "启动应用",
                "app启动",
                "打开app",
                "打开应用",
                "拉起app"
            ]
        },
        "app_launch_success": {
            "aliases": [
                "启动成功",
                "应用启动成功",
                "app启动成功"
            ]
        },
        "payment_success": {
            "aliases": [
                "支付成功",
                "支付完成",
                "payment success"
            ]
        },
        "video_play_start": {
            "aliases": [
                "开始播放",
                "视频开始播放",
                "video play start"
            ]
        },
        "user_signup": {
            "aliases": [
                "用户注册",
                "注册成功",
                "signup"
            ]
        },
    }

    matcher = build_event_matcher_from_catalog(mock_catalog_events)

    test_queries = [
        "app_launch",
        "应用启动",
        "app启动",
        "打开app",
        "启动应用",
        "应用启动成功",
        "支付成功",
        "开始播放",
        "用户注册",
        "launch",
        "payment success",
        "video play",
        "注册",
    ]

    for q in test_queries:
        result = matcher.match(q)
        print("=" * 100)
        print("query:", q)
        print("matched:", result.matched)
        print("score:", result.score)
        print("explain:", result.explain)
