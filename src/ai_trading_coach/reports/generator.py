from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from typing import Iterable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from ai_trading_coach.analysis.events import EventLevel, TradeEvent, detect_events_for_lifecycles
from ai_trading_coach.domain.trade_lifecycle import TradeLifecycle


@dataclass(frozen=True)
class DisciplineScore:
    score: int
    breakdown: dict[str, int]


def compute_discipline_score(events: Iterable[TradeEvent]) -> DisciplineScore:
    """
    Deterministic scoring:
    - Start 100
    - P0: -20 each
    - P1: -8 each
    - P2: 0 (informational)
    - Floor at 0
    """
    score = 100
    breakdown: dict[str, int] = {"base": 100, "P0": 0, "P1": 0, "P2": 0}
    for e in events:
        if e.level == EventLevel.P0:
            score -= 20
            breakdown["P0"] -= 20
        elif e.level == EventLevel.P1:
            score -= 8
            breakdown["P1"] -= 8
        else:
            breakdown["P2"] += 0
    if score < 0:
        score = 0
    return DisciplineScore(score=score, breakdown=breakdown)

def _penalty_reason_lines(events: list[TradeEvent]) -> list[str]:
    """
    Only keep what changes tomorrow's behavior:
    - reason (event name)
    - count
    - penalty magnitude
    """
    points = {EventLevel.P0: 20, EventLevel.P1: 8}
    agg: dict[tuple[EventLevel, str], int] = {}
    for e in events:
        if e.level not in (EventLevel.P0, EventLevel.P1):
            continue
        key = (e.level, e.name_zh)
        agg[key] = agg.get(key, 0) + 1

    items: list[tuple[int, EventLevel, str, int]] = []
    for (lvl, name), cnt in agg.items():
        penalty = cnt * points[lvl]
        items.append((penalty, lvl, name, cnt))
    items.sort(key=lambda x: (-x[0], 0 if x[1] == EventLevel.P0 else 1, x[2]))

    out: list[str] = []
    for penalty, lvl, name, cnt in items:
        out.append(f"- {lvl.value} {name} x{cnt}（-{penalty}）")
    return out


def _penalty_reason_summary(events: list[TradeEvent], *, top_n: int = 2) -> str:
    """
    One-line penalty summary. No engineer-y breakdown.
    """
    lines = _penalty_reason_lines(events)
    if not lines:
        return "无（没证据就不扣）"
    cleaned = [x[2:] if x.startswith("- ") else x for x in lines[:top_n]]
    if len(lines) > top_n:
        cleaned.append(f"…另有{len(lines)-top_n}项")
    return "；".join(cleaned)


def _to_tz(dt: datetime | None, tz_name: str) -> datetime | None:
    if not dt:
        return None
    if ZoneInfo is None:
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return dt


def _fmt_hhmm(dt: datetime | None, *, tz_name: str = "America/New_York") -> str:
    dt2 = _to_tz(dt, tz_name)
    if not dt2:
        return "--:--"
    try:
        return dt2.strftime("%H:%M")
    except Exception:
        return "--:--"


def summarize_risk_signals(events: Iterable[TradeEvent]) -> list[str]:
    """
    Deterministic summary strings (Chinese), derived from event types.
    """
    counts = Counter([e.event_type.value for e in events if e.level in (EventLevel.P0, EventLevel.P1)])
    signals: list[str] = []
    for k, v in counts.most_common():
        signals.append(f"{k} x{v}")
    return signals


def _events_stats(events: list[TradeEvent]) -> dict[str, int]:
    c = Counter([e.level.value for e in events])
    return {"P0": c.get("P0", 0), "P1": c.get("P1", 0), "P2": c.get("P2", 0), "total": len(events)}

def _format_evidence_brief(e: TradeEvent) -> str:
    """
    Short, accountable evidence string (Chinese). Always derived from event.evidence fields.
    """
    ev = e.evidence or {}
    t = e.event_type.value

    def _short_ts(ts: str | None) -> str:
        if not ts:
            return "NA"
        try:
            d, tpart = ts.split("T", 1)
            return f"{d[5:10]} {tpart[0:5]}"
        except Exception:
            return ts[:16]

    if t == "night_trading_us_eastern":
        return f"美东开仓：{_short_ts(ev.get('entry_ts_us_eastern'))}\n规则：夜盘 22:00-06:00 禁开新仓"
    if t == "high_leverage_used":
        return f"effective_leverage={ev.get('effective_leverage')}｜阈值={ev.get('threshold')}（基准={ev.get('base_balance_source')}）"
    if t == "big_loss_pct_equity":
        return f"loss%={ev.get('loss_pct_of_base_balance')}｜阈值%={ev.get('threshold_pct')}（基准={ev.get('base_balance_source')}）"
    if t == "stop_loss_triggered":
        tf = ev.get("trigger_fill") or {}
        return f"计划止损={ev.get('planned_stop_loss')}｜触发成交 price={tf.get('price')} amount={tf.get('amount')}"
    if t == "consecutive_losses":
        return f"n={ev.get('n')}（最近N笔都亏）"

    # Default: dump compact json (still structured evidence)
    return "证据：见事件 evidence（本报告已做精简展示）"


def _viper_comments(lifecycles: list[TradeLifecycle], events: list[TradeEvent]) -> list[str]:
    """
    Cold coach, no fluff. Each line must reference concrete metrics or event evidence.
    """
    by_lc: dict[str, list[TradeEvent]] = defaultdict(list)
    for e in events:
        if e.level in (EventLevel.P0, EventLevel.P1):
            by_lc[e.lifecycle_id].append(e)

    lines: list[str] = []
    # Sort by worst first: P0 > P1, then by timestamp
    all_lcs = sorted(
        lifecycles,
        key=lambda lc: (
            -sum(1 for e in by_lc.get(lc.lifecycle_id, []) if e.level == EventLevel.P0),
            -sum(1 for e in by_lc.get(lc.lifecycle_id, []) if e.level == EventLevel.P1),
            lc.metrics.entry_ts or datetime.min,
        ),
    )

    def pick_variant(key: str, variants: list[str]) -> str:
        idx = abs(hash(key)) % len(variants)
        return variants[idx]

    for lc in all_lcs:
        lc.recompute()
        pnl = lc.metrics.realized_pnl_usdt
        evs = by_lc.get(lc.lifecycle_id, [])
        sym = lc.symbol
        side = lc.position_side.value

        # One line per trade, short, blunt, actionable.
        if pnl is None:
            variants = [
                "还没结算就别讲复盘：先把这笔平掉，或者承认你在赌。",
                "pnl 为空=你没关账。明天第一件事：把“何时算结束”写成规则。",
                "你连输赢都没落到数据口径，还谈什么纪律？先补齐结算口径。",
            ]
            idx = abs(hash(lc.lifecycle_id)) % len(variants)
            lines.append(f"- 🧾 **{sym} {side}**：{variants[idx]}")
            continue

        if any(e.event_type.value == "night_trading_us_eastern" for e in evs):
            variants = [
                "夜盘开仓就是自找麻烦：22:00-06:00 禁开新仓，写进硬约束。",
                "夜盘开仓=自曝弱点。改法不复杂：夜盘不下手。",
                "别把夜盘当训练场，你是在练怎么亏钱：夜盘禁开新仓。",
            ]
            lines.append(f"- 🌙 **{sym} {side}**：{pick_variant(lc.lifecycle_id, variants)}")
            continue

        if pnl < 0:
            variants = [
                "亏损不是委屈，是账单。改法：进场前写死退出条件（止损/超时/撤退）。",
                "输钱不是问题，没规则才是。改法：单笔最大亏损阈值固定，触发就停。",
                "这笔亏损在提醒你：先控风险，再谈收益。改法：把杠杆/仓位上限写死。",
            ]
            lines.append(f"- 🔻 **{sym} {side}**：{pick_variant(lc.lifecycle_id, variants)}（pnl={pnl}）")
            continue

        variants = [
            "盈利不等于纪律。改法：把今天没出事的步骤写成检查清单，下次照抄。",
            "这笔赚到的是结果，不是能力。改法：把可复制动作固化，别靠运气。",
            "别庆祝，写总结。改法：明确哪一步是纪律贡献的，下一次只重复那一步。",
        ]
        lines.append(f"- 🟢 **{sym} {side}**：{pick_variant(lc.lifecycle_id, variants)}（pnl={pnl}）")

    return lines


def _fmt_hhmm(dt: datetime | None) -> str:
    # Display in US/Eastern by default (review timezone).
    dt2 = _to_tz(dt, "America/New_York")
    if not dt2:
        return "--:--"
    try:
        return dt2.strftime("%H:%M")
    except Exception:
        return "--:--"


def _fmt_price(x: Decimal | None) -> str:
    if x is None:
        return "NA"
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if len(s) > 14:
        return f"{x:.6g}"
    return s


def _fmt_amt(x: Decimal | None) -> str:
    if x is None:
        return "NA"
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if len(s) > 14:
        return f"{x:.6g}"
    return s


def _top_costly_mistakes(lifecycles: list[TradeLifecycle], top_n: int = 5) -> list[dict[str, str]]:
    """
    "Most expensive mistakes": rank by realized loss magnitude (USDT).
    """
    rows: list[dict[str, str]] = []
    losers = []
    for lc in lifecycles:
        lc.recompute()
        pnl = lc.metrics.realized_pnl_usdt
        if pnl is None:
            continue
        if pnl < 0:
            losers.append((abs(pnl), lc))
    losers.sort(key=lambda x: x[0], reverse=True)
    for loss_abs, lc in losers[:top_n]:
        rows.append(
            {
                "lifecycle_id": lc.lifecycle_id,
                "symbol": lc.symbol,
                "side": lc.position_side.value,
                "loss_usdt": str(loss_abs),
                "entry_ts": (lc.metrics.entry_ts.isoformat() if lc.metrics.entry_ts else ""),
                "exit_ts": (lc.metrics.exit_ts.isoformat() if lc.metrics.exit_ts else ""),
            }
        )
    return rows


def _behavior_patterns(events: list[TradeEvent]) -> dict[str, int]:
    """
    Simple deterministic pattern counts by event_type (P0+P1).
    """
    c = Counter([e.event_type.value for e in events if e.level in (EventLevel.P0, EventLevel.P1)])
    return dict(c)


def _hard_constraints_suggestions(pattern_counts: dict[str, int]) -> list[str]:
    """
    Hard constraints (Chinese). Not price prediction, not trade advice.
    Triggered deterministically based on observed patterns.
    """
    out: list[str] = []
    if pattern_counts.get("night_trading_us_eastern", 0) > 0:
        out.append("硬约束：从现在起，禁止美东22:00-06:00开新仓（你已经夜盘开仓了）。")
    if pattern_counts.get("high_leverage_used", 0) > 0:
        out.append("硬约束：有效杠杆上限=10x（你已经用过高杠杆了）。")
    if pattern_counts.get("big_loss_pct_equity", 0) > 0:
        out.append("硬约束：单笔最大允许亏损=可用保证金的3%（你已经出现单笔大亏了）。")
    if pattern_counts.get("consecutive_losses", 0) > 0:
        out.append("硬约束：连续亏损触发后冷静期=24小时不交易（你已经连续亏损了）。")
    return out


def generate_daily_report_md(*, period_start: datetime, period_end: datetime, lifecycles: list[TradeLifecycle]) -> str:
    events = detect_events_for_lifecycles(lifecycles)
    stats = _events_stats(events)
    score = compute_discipline_score(events)
    p0p1 = [e for e in events if e.level in (EventLevel.P0, EventLevel.P1)]
    p0p1.sort(key=lambda e: (0 if e.level == EventLevel.P0 else 1, e.occurred_at))
    patterns = _behavior_patterns(p0p1)
    hard_constraints = _hard_constraints_suggestions(patterns)
    penalty_summary = _penalty_reason_summary(events)

    lines: list[str] = []
    lines.append(f"## 🧾 每日复盘（美东） {period_start.date().isoformat()}")
    lines.append("")

    # ① 今日裁决（强制阅读 1-3 行）
    lines.append("### ① 今日裁决（强制阅读）")
    lines.append(f"- 今日裁决：P0={stats['P0']}，P1={stats['P1']}。别解释。")
    if hard_constraints:
        # keep only one: the next-day action
        lines.append(f"- 明日只改一件事：{hard_constraints[0]}")
    else:
        lines.append("- 明日只改一件事：无（今天没证据触发硬约束）")
    lines.append("")

    # ② 纪律分 & 扣分原因（只讲原因+幅度；删掉工程细节）
    lines.append("### ② 纪律分 & 扣分原因")
    lines.append(f"- 纪律分：**{score.score}/100**")
    lines.append(f"- 扣分：{penalty_summary}")
    lines.append("")

    # ③ 证据与追责（附件可折叠，展示极简）
    lines.append("### ③ 证据与追责（需要时再看）")
    if p0p1:
        # keep only the minimum: up to 3 events
        for e in p0p1[:3]:
            lines.append(f"- {e.level.value} {e.name_zh}｜{e.symbol}")
            for ln in _format_evidence_brief(e).splitlines()[:2]:
                lines.append(f"  - {ln}")
        if len(p0p1) > 3:
            lines.append(f"- … 其余 {len(p0p1) - 3} 条事件略")
    else:
        lines.append("- 无P0/P1（没证据就不判）")
    lines.append("")

    # 交易列表（极简：开平时间简写 + 开平价 + 量 + 盈亏%保证金）
    lines.append("### 📒 今日交易（30秒扫完）")
    if not lifecycles:
        lines.append("- 无")
    else:
        shown = 0
        # Closed first, then open; stable sort by entry time.
        lifecycles_sorted = sorted(
            lifecycles,
            key=lambda lc: (0 if lc.status == "closed" else 1, lc.metrics.entry_ts or datetime.min),
        )
        for lc in lifecycles_sorted:
            if shown >= 8:
                break
            lc.recompute()
            entry_t = _fmt_hhmm(lc.metrics.entry_ts)
            exit_t = _fmt_hhmm(lc.metrics.exit_ts)
            entry_px = _fmt_price(lc.metrics.entry_avg_price)
            exit_px = _fmt_price(lc.metrics.exit_avg_price)
            qty = _fmt_amt(lc.metrics.max_abs_position_amount)
            pnl = lc.metrics.realized_pnl_usdt
            pnl_pct = lc.metrics.realized_pnl_pct_of_available_margin
            status = "✅已平" if lc.status == "closed" else "🕗未平"
            pnl_str = "未结算" if pnl is None else f"{pnl:.2f}U"
            pct_str = "" if pnl_pct is None else f"（{pnl_pct:.2f}%保证金）"
            # Open trades: hide exit time/price to reduce noise
            if lc.status != "closed":
                lines.append(f"- {status} {lc.symbol} {lc.position_side.value}｜{entry_t}@{entry_px}｜量≈{qty}｜{pnl_str}")
            else:
                lines.append(f"- {status} {lc.symbol} {lc.position_side.value}｜{entry_t}@{entry_px}→{exit_t}@{exit_px}｜量≈{qty}｜{pnl_str}{pct_str}")
            shown += 1
        if len(lifecycles) > shown:
            lines.append(f"- … 其余 {len(lifecycles) - shown} 笔略")
    lines.append("")

    # Coach: only 3 short lines max
    lines.append("### 🐍 教练（1分钟知道明天怎么改）")
    for c in _viper_comments(lifecycles, p0p1)[:3]:
        lines.append(c)
    lines.append("")
    lines.append("- 全量证据在本地 SQLite：`lifecycles.data_json`（fills含 trade_id/order_id）")
    return "\n".join(lines)


def generate_periodic_report_md(
    *,
    title_zh: str,
    period_start: datetime,
    period_end: datetime,
    lifecycles: list[TradeLifecycle],
) -> str:
    """
    Weekly / Monthly report template.
    """
    events = detect_events_for_lifecycles(lifecycles)
    stats = _events_stats(events)
    patterns = _behavior_patterns(events)
    top5 = _top_costly_mistakes(lifecycles, top_n=5)
    hard_constraints = _hard_constraints_suggestions(patterns)

    lines: list[str] = []
    lines.append(f"## {title_zh}（美东）")
    lines.append(f"- 周期：{period_start.isoformat()} ~ {period_end.isoformat()}")
    lines.append("")
    lines.append("### 事件统计")
    lines.append(f"- P0={stats['P0']} / P1={stats['P1']} / P2={stats['P2']}（共 {stats['total']}）")
    lines.append("")
    lines.append("### 行为模式统计（P0/P1，只算证据）")
    if patterns:
        for k, v in sorted(patterns.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("### 最昂贵的错误 Top5（按亏损额）")
    if top5:
        for r in top5:
            lines.append(
                f"- {r['lifecycle_id']} | {r['symbol']} | {r['side']} | loss={r['loss_usdt']} | "
                f"{r['entry_ts']} -> {r['exit_ts']}"
            )
    else:
        lines.append("- 无（或缺少pnl）")
    lines.append("")
    lines.append("### 纪律趋势变化")
    lines.append("- （预留）将按周/月对 P0/P1 的频次与纪律评分趋势做对比图/表。")
    lines.append("")
    lines.append("### 下一个周期硬约束（命令，不是建议）")
    if hard_constraints:
        for s in hard_constraints:
            lines.append(f"- {s}")
    else:
        lines.append("- 无（目前没有足够证据触发硬约束）")
    lines.append("")
    lines.append("### 证据索引")
    lines.append("- 所有结论必须能在事件 evidence 与 lifecycle 的 fills 中找到对应证据。")
    return "\n".join(lines)


