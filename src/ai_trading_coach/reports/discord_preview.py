from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from ai_trading_coach.analysis.events import EventLevel, TradeEvent


@dataclass(frozen=True)
class Preview:
    title: str
    body: str

    def render(self) -> str:
        # Title first line; body below. Keep it short.
        if self.body.strip():
            return f"**{self.title}**\n{self.body.strip()}"
        return f"**{self.title}**"


def _stats(events: Iterable[TradeEvent]) -> dict[str, int]:
    c = Counter([e.level.value for e in events])
    return {"P0": c.get("P0", 0), "P1": c.get("P1", 0), "P2": c.get("P2", 0)}

def _short_id(event_id: str, keep: int = 48) -> str:
    if len(event_id) <= keep:
        return event_id
    return event_id[:keep] + "…"

def make_review_preview(*, kind_zh: str, date_label: str, events: list[TradeEvent], discipline_score: int) -> Preview:
    """
    Viper-coach tone: short, blunt, evidence-driven. No encouragement.
    """
    s = _stats(events)

    # Keep preview 3 lines (forced-read). No IDs. No long lists.
    issues = [e for e in events if e.level in (EventLevel.P0, EventLevel.P1)]
    issues.sort(key=lambda e: (0 if e.level == EventLevel.P0 else 1, e.event_type.value))
    top = issues[:1]

    line1 = f"📌 今日裁决：P0={s['P0']} P1={s['P1']}（别解释）"
    if top:
        line2 = f"🔥 扣分主因：{top[0].name_zh}（{top[0].symbol}）"
    else:
        line2 = "🔥 扣分主因：无P0/P1（没证据就不判）"
    line3 = f"🎯 纪律分：{discipline_score}/100｜明日只改一件事：按硬约束执行"

    return Preview(title=f"🧾 {kind_zh}｜{date_label}", body="\n".join([line1, line2, line3]))


