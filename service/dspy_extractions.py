"""
DSPy 实体提取模块

使用 DSPy 框架实现 LLM 实体提取，替代传统的 LangChain + 手写 prompt 方式
"""

import json
import os
from typing import List, Optional

import dspy
from dotenv import load_dotenv

load_dotenv()

# ==================== DSPy Signature 定义 ====================
class ExtractionSignature(dspy.Signature):
    """从用户问题中提取结构化信息"""
    query = dspy.InputField(desc="用户的自然语言查询")
    metric_extractions = dspy.OutputField(desc="提取的指标列表，JSON字符串格式: [{'text':'PV'}]")
    time_extractions = dspy.OutputField(desc="提取的时间范围列表，JSON字符串格式")
    event_extractions = dspy.OutputField(desc="提取的事件列表，JSON字符串格式")
    region_filter = dspy.OutputField(desc="区域代码列表 (EUTTP/USTTP/ROW)，JSON字符串格式")
    group_by_extractions = dspy.OutputField(desc="提取的分组维度列表，JSON字符串格式")


# ==================== DSPy Module 定义 ====================
class QueryExtraction(dspy.Module):
    """查询实体提取模块"""

    def __init__(self):
        super().__init__()
        self.extract = dspy.Predict(ExtractionSignature)

    def forward(self, query: str):
        """执行实体提取"""
        prediction = self.extract(query=query)
        return dspy.Prediction(
            query=query,
            metric_extractions=prediction.metric_extractions,
            time_extractions=prediction.time_extractions,
            event_extractions=prediction.event_extractions,
            region_filter=prediction.region_filter,
            group_by_extractions=prediction.group_by_extractions
        )


# ==================== 全局配置 ====================
_compiled_module: Optional[QueryExtraction] = None
_use_dspy = os.getenv("USE_DSPY", "false").lower() == "true"


def configure_dspy():
    """
    配置 DSPy，支持 Ollama 和智谱 AI

    通过 LLM_BACKEND 环境变量切换:
    - ollama: 使用本地 Ollama 模型
    - zhipu: 使用智谱 AI API (默认)
    """
    backend = os.getenv("LLM_BACKEND", "zhipu").lower()

    if backend == "ollama":
        model = os.getenv("OLLAMA_MODEL", "glm4")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        lm = dspy.Ollama(
            model=model,
            base_url=base_url,
        )
    else:  # zhipu (默认)
        api_key = os.getenv("ZHIPUAI_API_KEY")
        model = os.getenv("ZHIPU_MODEL", "glm-4")
        if not api_key:
            raise ValueError("ZHIPUAI_API_KEY 环境变量未设置")
        lm = dspy.OpenAI(
            api_base="https://open.bigmodel.cn/api/paas/v4",
            api_key=api_key,
            model=model
        )

    dspy.settings.configure(lm=lm)


def compile_with_bootstrap(trainset, max_bootstrapped_demos=4, max_labeled_demos=7):
    """
    使用 BootstrapFewShot 优化器编译模块

    Args:
        trainset: 训练数据集
        max_bootstrapped_demos: 最多引导示例数
        max_labeled_demos: 最多标记示例数
    """
    from dspy.teleprompt import BootstrapFewShot

    from service.dspy_trainset import validate_extraction

    global _compiled_module

    # 配置 DSPy
    configure_dspy()

    # 创建优化器
    teleprompter = BootstrapFewShot(
        metric=validate_extraction,
        max_bootstrapped_demos=max_bootstrapped_demos,
        max_labeled_demos=max_labeled_demos
    )

    # 编译模块
    _compiled_module = teleprompter.compile(
        QueryExtraction(),
        trainset=trainset
    )

    return _compiled_module


def get_compiled_module() -> QueryExtraction:
    """获取编译后的模块"""
    if _compiled_module is None:
        # 首次使用时自动编译
        from service.dspy_trainset import get_trainset
        trainset = get_trainset()
        compile_with_bootstrap(trainset)
    return _compiled_module


def extract_dspy(query: str) -> dict:
    """
    使用 DSPy 进行实体提取

    Args:
        query: 用户查询文本

    Returns:
        dict: 提取结果的字典格式
    """
    # 如果未编译，先编译
    module = get_compiled_module()

    # 执行提取
    result = module(query=query)

    # 转换为字典格式
    return {
        "metric_extractions": json.loads(result.metric_extractions),
        "time_extractions": json.loads(result.time_extractions),
        "event_extractions": json.loads(result.event_extractions),
        "region_filter": json.loads(result.region_filter),
        "group_by_extractions": json.loads(result.group_by_extractions),
    }


def is_dspy_enabled() -> bool:
    """检查是否启用 DSPy"""
    return _use_dspy


def enable_dspy():
    """启用 DSPy"""
    global _use_dspy
    _use_dspy = True


def disable_dspy():
    """禁用 DSPy"""
    global _use_dspy
    _use_dspy = False


# ==================== 便捷函数 ====================
def extract(query: str, use_dspy: Optional[bool] = None) -> dict:
    """
    统一的提取接口

    Args:
        query: 用户查询文本
        use_dspy: 是否使用 DSPy（None 则使用全局配置）

    Returns:
        dict: 提取结果
    """
    if use_dspy is None:
        use_dspy = _use_dspy

    if use_dspy:
        return extract_dspy(query)
    else:
        # 回退到 LangChain 实现
        from service.llm_extractions import extract_llm
        result = extract_llm(query)
        return {
            "metric_extractions": [e.model_dump() for e in result.metric_extractions],
            "time_extractions": [e.model_dump() for e in result.time_extractions],
            "event_extractions": [e.model_dump() for e in result.event_extractions],
            "region_filter": result.region_filter,
            "group_by_extractions": [e.model_dump() for e in result.group_by_extractions],
        }
