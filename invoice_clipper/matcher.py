"""
凑票模块 — 给定目标金额，找出最接近且不超过的发票组合（v1.0）

算法：
- 使用 DP（子集和问题）寻找 ≤ 目标金额的最大和组合
- 支持发票数量限制
- 提供多组候选结果供用户选择
"""
import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def find_best_match(
    invoices: List[dict],
    target_amount: float,
    max_count: int = 20,
) -> List[dict]:
    """
    找出最接近 target_amount 且不超过的发票组合。

    Args:
        invoices: 候选发票列表（每项需有 id, amount_with_tax 字段）
        target_amount: 目标报销金额
        max_count: 最多使用的发票张数（防止 DP 组合爆炸）

    Returns:
        选中的发票列表（按金额降序排列），如无匹配返回 []
    """
    if not invoices or target_amount <= 0:
        return []

    # 过滤无效发票
    valid = [inv for inv in invoices if (inv.get("amount_with_tax") or 0) > 0]
    if not valid:
        return []

    # 按金额升序排列
    valid_sorted = sorted(valid, key=lambda x: x["amount_with_tax"])

    # 如果单张最大金额已 ≤ 目标金额，先考虑单张最优
    # 同时生成 DP 候选

    # 使用 DP 求解子集和问题
    # dp[s] = used_count: 达到金额 s 所需的最少发票张数
    # 限制发票张数以控制计算量
    n = min(len(valid_sorted), 60)  # 最多考虑 60 张
    candidates = valid_sorted[:n]

    # 将金额转为整数分（避免浮点误差）
    scale = 100
    target_cents = int(round(target_amount * scale))
    amounts_cents = [int(round(inv["amount_with_tax"] * scale)) for inv in candidates]

    # DP: dp[金额] = 发票索引列表
    # 用字典避免稀疏数组过大
    dp: Dict[int, List[int]] = {0: []}
    best_sum = 0
    best_indices: List[int] = []

    for i, cents in enumerate(amounts_cents):
        if cents > target_cents:
            continue
        # 反向遍历当前 dp 键，避免同一张发票重复使用
        current_sums = list(dp.keys())
        for s in current_sums:
            new_sum = s + cents
            if new_sum > target_cents:
                continue
            new_indices = dp[s] + [i]
            if len(new_indices) > max_count:
                continue
            # 记录更优解：金额更大优先，金额相同张数更少优先
            if (new_sum not in dp
                    or new_sum > best_sum
                    or (new_sum == best_sum and len(new_indices) < len(dp.get(best_sum, [])))
                    or (new_sum > best_sum and len(new_indices) <= len(dp.get(best_sum, [])))):
                dp[new_sum] = new_indices
                if new_sum > best_sum:
                    best_sum = new_sum
                    best_indices = new_indices

    # 构建返回结果
    result = [candidates[i] for i in best_indices]
    # 按金额降序排列（大额在前）
    result.sort(key=lambda x: x["amount_with_tax"], reverse=True)

    if not result:
        # 退而求其次：选择单张最接近的发票
        singles = [inv for inv in valid_sorted if inv["amount_with_tax"] <= target_amount]
        if singles:
            result = [singles[-1]]  # 金额最大的单张

    return result


def find_multiple_candidates(
    invoices: List[dict],
    target_amount: float,
    count: int = 3,
    max_count: int = 20,
) -> List[Tuple[List[dict], float]]:
    """
    找出多组候选凑票方案。

    Args:
        invoices: 候选发票列表
        target_amount: 目标金额
        count: 返回几组候选
        max_count: 每组的最大发票张数

    Returns:
        [(发票列表, 合计金额), ...]，按合计金额降序排列
    """
    if not invoices or target_amount <= 0:
        return []

    valid = [inv for inv in invoices if (inv.get("amount_with_tax") or 0) > 0]
    if not valid:
        return []

    # 先找最优解
    best = find_best_match(valid, target_amount, max_count)

    result = []
    used_ids = set()

    if best:
        total = sum(inv["amount_with_tax"] for inv in best)
        result.append((best, total))
        used_ids.update(inv["id"] for inv in best)

    # 尝试排除最优解中的某张发票，找次优解
    if best and len(best) > 1:
        for skip_idx in range(len(best)):
            skip_id = best[skip_idx]["id"]
            remaining = [inv for inv in valid if inv["id"] != skip_id]
            alt = find_best_match(remaining, target_amount, max_count)
            if alt:
                alt_total = sum(inv["amount_with_tax"] for inv in alt)
                # 去重
                alt_ids = frozenset(inv["id"] for inv in alt)
                if alt_ids not in {frozenset(inv["id"] for inv in r[0]) for r in result}:
                    result.append((alt, alt_total))
                    if len(result) >= count:
                        break

    # 按合计金额降序排列
    result.sort(key=lambda x: x[1], reverse=True)

    # 截取需要的数量
    return result[:count]


def find_multiple_candidates_with_required(
    invoices: List[dict],
    target_amount: float,
    required: List[dict],
    count: int = 3,
    max_count: int = 20,
) -> List[Tuple[List[dict], float]]:
    """
    凑票：必须包含指定的发票。

    先扣除必选发票的金额，再用剩余发票凑剩余目标金额，
    最后把必选发票合并到每个候选方案中。

    Args:
        invoices: 候选发票池
        target_amount: 目标报销金额
        required: 必须包含的发票列表
        count: 返回几组候选
        max_count: 每组的最大发票张数（含必选项）

    Returns:
        [(发票列表, 合计金额), ...]，按合计金额降序排列
    """
    if not invoices or target_amount <= 0:
        return []

    required = [inv for inv in required if (inv.get("amount_with_tax") or 0) > 0]
    required_total = sum(inv["amount_with_tax"] for inv in required)

    # 必选金额已超目标：仅返回必选方案，由调用方决定是否接受
    if required_total > target_amount:
        return [(required, required_total)] if required else []

    remaining_target = target_amount - required_total
    required_ids = {inv["id"] for inv in required}
    remaining_pool = [inv for inv in invoices if inv["id"] not in required_ids]
    remaining_max_count = max(0, max_count - len(required))

    # 必选即已凑满或剩余资源耗尽：直接返回仅含必选的方案
    if remaining_target <= 0 or not remaining_pool or remaining_max_count == 0:
        return [(required, required_total)] if required else []

    sub_results = find_multiple_candidates(
        remaining_pool, remaining_target,
        count=count, max_count=remaining_max_count,
    )

    if not sub_results:
        # 剩余池中找不到合规组合，仅返回必选
        return [(required, required_total)]

    # 合并必选 + 各候选方案
    combined: List[Tuple[List[dict], float]] = []
    seen: set = set()
    for sub_invoices, sub_total in sub_results:
        merged = required + sub_invoices
        merged.sort(key=lambda x: x["amount_with_tax"], reverse=True)
        key = frozenset(inv["id"] for inv in merged)
        if key in seen:
            continue
        seen.add(key)
        combined.append((merged, required_total + sub_total))

    return combined[:count]
