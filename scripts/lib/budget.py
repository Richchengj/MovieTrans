"""时长预算与变速规划。

预算 = 原句时长 + 可借用的前后静音间隙（有上限）。
若生成的配音仍超出预算，用变速吸收；超出变速上限则标记 overrun 交人工复审。
"""
from __future__ import annotations

from .schemas import Segment


def compute_budget(
    seg: Segment,
    prev_seg: Segment | None,
    next_seg: Segment | None,
    config: dict,
) -> float:
    """计算该段允许占用的时间窗（秒）。

    硬约束：不得越过下一段（任何说话人）的起点——配音叠到下一句上
    比语速略快更伤听感。前侧借用（推后起点）在起点固定的对齐策略下
    不生效，但保留计算以便截断前不改变既有行为。
    """
    base = seg.end - seg.start
    max_borrow = float(config["align"]["max_gap_borrow"])
    borrow = 0.0

    if prev_seg is not None:
        gap = seg.start - prev_seg.end
        borrow += min(max(gap, 0.0) / 2.0, max_borrow / 2.0)
    if next_seg is not None:
        gap = next_seg.start - seg.end
        borrow += min(max(gap, 0.0) / 2.0, max_borrow / 2.0)

    budget = base + borrow
    if next_seg is not None:
        # 硬窗截断：预算永不越过下一段起点；原声本身超窗（ASR 段重叠等
        # 源数据问题）时保底给 base，不放大问题
        hard_window = max(next_seg.start - seg.start, base)
        budget = min(budget, hard_window)

    return round(budget, 3)


def compute_budgets(segments: list[Segment], config: dict) -> dict[str, float]:
    """按全局时间轴的相邻关系，批量计算每段预算。

    相邻判断必须看**任何说话人**的段（而不仅是同说话人）：多人交替对白时，
    同说话人"下一段"之前往往插着别人的对白，把那段空隙借进来会直接侵占
    他人对白时间（20261020_bcs_multi 实测 4 处时间轴碰撞的根因）。
    全局相邻后，借用的 gap 永远是没人占用的真实空隙。
    """
    budgets: dict[str, float] = {}
    ordered = sorted(segments, key=lambda s: s.start)
    for i, seg in enumerate(ordered):
        prev_seg = ordered[i - 1] if i > 0 else None
        next_seg = ordered[i + 1] if i + 1 < len(ordered) else None
        budgets[seg.id] = compute_budget(seg, prev_seg, next_seg, config)

    return budgets


def plan_speed(input_dur: float, budget: float, config: dict) -> tuple[float, list[str]]:
    """返回 (变速比, 标记列表)。

    变速比 > 1 表示需要加速（配音偏长）。偏短不放慢：把语速拖到 0.85 倍
    既抵消 s07 的原生提速、又吃掉句间留白，改为保持合成语速，尾部留白
    自然成为与下一句的间隔。
    """
    if input_dur <= 0 or budget <= 0:
        return 1.0, ["empty"]

    speed = max(input_dur / budget, 1.0)
    hi = float(config["align"]["max_speed"])
    flags: list[str] = []

    if speed > hi:
        speed = hi
        flags.append("overrun")

    return round(speed, 4), flags


# ---------- 极端段救援（2026-10-21） ----------

def timeline_room(
    seg_id: str,
    starts: dict[str, float],
    ends: dict[str, float],
    ordered_ids: list[str],
    config: dict,
    prev_extreme: bool = False,
) -> tuple[float, float, float]:
    """该段在时间轴上的真实可用空间。

    返回 (window, pre_borrow, next_window)：
      window      = 下一句起点 - gap_buffer - 本段起点（+ 可向前借的部分）
      pre_borrow  = 实际向前借走的秒数（起点前移量，≤ pre_borrow_max）
      next_window = 下一句自身的窗口（叠话上限用，防止埋掉下一句）

    前借仅在救援段启用：上一段结尾留有空档才借，且上一段本身也是极端段
    （可能叠话伸过来）时不借，避免三重重叠。
    """
    cfg_align = config["align"]
    buffer = float(cfg_align.get("gap_buffer", 0.10))
    pre_max = float(cfg_align.get("pre_borrow_max", 0.25))
    if seg_id not in starts or seg_id not in ordered_ids:
        return 0.0, 0.0, 0.0
    i = ordered_ids.index(seg_id)
    start = starts[seg_id]

    pre = 0.0
    if not prev_extreme and i > 0:
        gap_before = start - ends[ordered_ids[i - 1]]
        pre = max(0.0, min(gap_before - 0.05, pre_max))

    if i + 1 < len(ordered_ids):
        next_id = ordered_ids[i + 1]
        next_start = starts[next_id]
        window = max(next_start - buffer - start, 0.0) + pre
        if i + 2 < len(ordered_ids):
            next_window = max(starts[ordered_ids[i + 2]] - next_start, 0.0)
        else:
            next_window = float("inf")
    else:
        window = float("inf")  # 片尾无邻句，不设窗口上限
        next_window = 0.0

    return window, pre, next_window


def plan_rescue(
    dur: float,
    budget: float,
    window: float,
    next_window: float,
    config: dict,
    chars: int = 0,
) -> tuple[float, str | None]:
    """极端段的拟合目标与救援方式。

    返回 (target, rescue)：
      target  = 重生成/变速的目标时长（秒）
      rescue  = None（不救）/ "gap"（借空档）/ "overlap"（与下一句叠话）

    决策顺序（2026-10-21 下午修订：**借为主、叠话兜底**——用户实测叠话有
    明显听感不适，能借就尽量借，叠话只在实在不行时最小量使用）：
      1) 纯借：目标 = min(窗口, 自然时长, 预算×rescue_max_ratio)，
         窗口内静音全用上（含前借）；只要**总变速** dur/target 不超
         rescue_speed_max 且**实际字/秒**不超 rescue_max_cps，就不叠话
         ——窗口内宁可快一点也不叠（相对自然合成的倍率高≠听感快：
         IndexTTS 自然合成偏慢，2.2x 约合中文 4~5 字/秒的日常语速）；
      2) 叠话：借满窗口总变速仍超上限时，只伸进「让总变速回落到上限」
         所需的最小量（≤overlap_max 且 ≤下一句窗口×ratio），尾淡出兜底；
      3) 叠话量不足 overlap_min → 仍按窗口压（rescue="gap"）。
    """
    cfg_align = config["align"]
    max_ratio = float(cfg_align.get("rescue_max_ratio", 2.5))
    speed_max = float(cfg_align.get("rescue_speed_max", 2.2))
    cps_max = float(cfg_align.get("rescue_max_cps", 7.0))
    ov_max = float(cfg_align.get("overlap_max", 1.2))
    ov_ratio = float(cfg_align.get("overlap_next_ratio", 0.5))
    ov_min = float(cfg_align.get("overlap_min", 0.30))

    cap = budget * max_ratio if budget > 0 else dur
    if window == float("inf"):
        return dur, "gap"  # 片尾：没有下一句可撞，按自然时长（不设拖沓封顶）

    # 阶段1：纯借（含必要提速）——目标取 窗口/自然/封顶 三者最小
    target = max(min(window, dur, cap), 0.0)
    if target <= 0:
        return target, "gap"
    speed_ok = dur / target <= speed_max + 1e-6
    cps_ok = chars <= 0 or chars / target <= cps_max
    if speed_ok and cps_ok:
        return target, "gap"

    # 阶段2：叠话兜底——最小量：总变速回落到 speed_max 即可，不多伸
    want = dur / speed_max
    ov_cap = ov_max
    if next_window and next_window != float("inf"):
        ov_cap = min(ov_cap, next_window * ov_ratio)
    ov = min(max(want - window, 0.0), ov_cap)
    if ov >= ov_min:
        return min(window + ov, dur), "overlap"
    return target, "gap"
