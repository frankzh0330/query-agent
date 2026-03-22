"""
DSPy 训练数据集

将现有的 few-shot 示例转换为 DSPy Example 格式
"""

import dspy

# 训练数据集 - 从现有 few-shot 示例转换
trainset = [
    dspy.Example(
        query="查询德国近7天 app_launch 的 PV",
        metric_extractions='[{"text":"PV"}]',
        time_extractions='[{"text":"近7天"}]',
        event_extractions='[{"text":"app_launch"}]',
        region_filter='["EUTTP"]',
        group_by_extractions='[]'
    ).with_inputs("query"),

    dspy.Example(
        query="加州的用户数",
        metric_extractions='[{"text":"用户数"}]',
        time_extractions='[]',
        event_extractions='[]',
        region_filter='["USTTP"]',
        group_by_extractions='[]'
    ).with_inputs("query"),

    dspy.Example(
        query="查看最近的访问量",
        metric_extractions='[{"text":"访问量"}]',
        time_extractions='[{"text":"最近"}]',
        event_extractions='[]',
        region_filter='["ROW"]',
        group_by_extractions='[]'
    ).with_inputs("query"),

    dspy.Example(
        query="欧洲市场的浏览量",
        metric_extractions='[{"text":"浏览量"}]',
        time_extractions='[]',
        event_extractions='[]',
        region_filter='["EUTTP"]',
        group_by_extractions='[]'
    ).with_inputs("query"),

    dspy.Example(
        query="show PV for Italy last 30 days",
        metric_extractions='[{"text":"PV"}]',
        time_extractions='[{"text":"last 30 days"}]',
        event_extractions='[]',
        region_filter='["EUTTP"]',
        group_by_extractions='[]'
    ).with_inputs("query"),

    dspy.Example(
        query="新加坡的数据",
        metric_extractions='[{"text":"PV"}]',
        time_extractions='[]',
        event_extractions='[]',
        region_filter='["ROW"]',
        group_by_extractions='[]'
    ).with_inputs("query"),

    dspy.Example(
        query="查询德国的数据",
        metric_extractions='[{"text":"PV"}]',
        time_extractions='[]',
        event_extractions='[]',
        region_filter='["EUTTP"]',
        group_by_extractions='[]'
    ).with_inputs("query"),
]


# 测试数据集（用于验证）
testset = [
    dspy.Example(
        query="法国新用户的注册趋势",
        metric_extractions='[{"text":"UV"}]',
        time_extractions='[]',
        event_extractions='[]',
        region_filter='["EUTTP"]',
        group_by_extractions='[]'
    ).with_inputs("query"),
]


def get_trainset():
    """获取训练数据集"""
    return trainset


def get_testset():
    """获取测试数据集"""
    return testset


def validate_extraction(example, pred, trace=None):
    """
    验证提取结果的准确性

    Args:
        example: DSPy Example，包含正确答案
        pred: 预测结果
        trace: 调试信息（可选）

    Returns:
        bool: 提取是否正确
    """
    try:
        import json

        # 检查每个字段是否匹配
        fields = ['metric_extractions', 'time_extractions', 'event_extractions',
                  'region_filter', 'group_by_extractions']

        for field in fields:
            expected = json.loads(getattr(example, field))
            actual = json.loads(getattr(pred, field))

            if expected != actual:
                return False

        return True
    except Exception:
        return False
