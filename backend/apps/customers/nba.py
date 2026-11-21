"""Next-best-action rules (pure; unit-tested). Inputs: RFM segment, rule segments, churn/CLV scores, order and
ticket counts. The first matching rule wins; every evaluated signal is returned as the explanation.

Thresholds are deliberately simple and explainable (no model): a churn probability of 0.6+ is "high risk", a CLV in
the top 20% of the scored customers is "high value", 3+ support tickets is "service issue".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HIGH_CHURN = 0.6
MEDIUM_CHURN = 0.35
HIGH_VALUE_PERCENTILE = 0.8
SERVICE_TICKETS = 3


@dataclass(frozen=True)
class Action:
    code: str
    title: str
    channel: str
    priority: str  # high | medium | low
    description: str


ACTIONS: dict[str, Action] = {a.code: a for a in (
    Action("service_recovery", "Service recovery call", "phone", "high",
           "Resolve the open service issues personally before making any offer."),
    Action("vip_retention_offer", "VIP retention offer", "account_manager", "high",
           "A high-value customer is likely to churn: personal outreach with a tailored retention offer."),
    Action("win_back_campaign", "Win-back campaign", "email", "high",
           "Likely to churn: send a time-limited win-back incentive."),
    Action("first_purchase_incentive", "First-purchase incentive", "email", "medium",
           "Known customer without orders: offer a first-purchase incentive."),
    Action("loyalty_reward", "Loyalty reward and referral ask", "email", "medium",
           "Best customers: thank them with a loyalty reward and ask for referrals."),
    Action("cross_sell", "Cross-sell complementary products", "email", "medium",
           "Loyal customers: recommend complementary products."),
    Action("membership_upsell", "Membership upsell", "email", "medium",
           "Promising customers: invite them to a membership or bundle."),
    Action("onboarding_nurture", "Onboarding nurture series", "email", "low",
           "New customers: guide them through the catalogue with an onboarding series."),
    Action("personalized_recommendation", "Personalised recommendations", "push", "medium",
           "Engagement is slipping: send personalised recommendations."),
    Action("reactivation_offer", "Reactivation offer", "sms", "high",
           "Previously valuable customers drifting away: send a reactivation offer."),
    Action("low_cost_reengagement", "Low-cost re-engagement", "email", "low",
           "Dormant customers: include them in low-cost re-engagement campaigns only."),
    Action("nurture_newsletter", "Newsletter nurture", "email", "low",
           "No strong signal: keep in the regular newsletter."),
)}

RFM_ACTION = {
    "Champions": "loyalty_reward",
    "Loyal customers": "cross_sell",
    "Potential loyalists": "membership_upsell",
    "New customers": "onboarding_nurture",
    "Need attention": "personalized_recommendation",
    "At risk": "reactivation_offer",
    "Hibernating": "low_cost_reengagement",
}


def churn_band(probability: float | None) -> str | None:
    if probability is None:
        return None
    if probability >= HIGH_CHURN:
        return "high"
    return "medium" if probability >= MEDIUM_CHURN else "low"


def value_percentiles(values: Mapping[Any, float]) -> dict[Any, float]:
    """key -> percentile rank in [0, 1] (ties share the lower rank; a single value is 1.0)."""
    if not values:
        return {}
    ordered = sorted(values.values())
    n = len(ordered)
    out = {}
    for key, v in values.items():
        below = sum(1 for x in ordered if x < v)
        out[key] = 1.0 if n == 1 else round(below / (n - 1), 4)
    return out


def decide(*, rfm_label: str | None, order_count: int, ticket_count: int = 0, churn_probability: float | None = None,
           clv_percentile: float | None = None, segments: Sequence[str] = ()) -> dict[str, Any]:
    """-> {action, title, channel, priority, description, rule, signals}."""
    band = churn_band(churn_probability)
    high_value = clv_percentile is not None and clv_percentile >= HIGH_VALUE_PERCENTILE
    signals = {"rfm_segment": rfm_label, "order_count": order_count, "ticket_count": ticket_count,
               "churn_probability": churn_probability, "churn_band": band, "clv_percentile": clv_percentile,
               "high_value": high_value, "segments": list(segments)}
    if ticket_count >= SERVICE_TICKETS and (band in ("high", "medium") or order_count > 0):
        code, rule = "service_recovery", f"ticket_count >= {SERVICE_TICKETS}"
    elif band == "high" and high_value:
        code, rule = "vip_retention_offer", f"churn >= {HIGH_CHURN} and clv percentile >= {HIGH_VALUE_PERCENTILE}"
    elif band == "high":
        code, rule = "win_back_campaign", f"churn >= {HIGH_CHURN}"
    elif order_count == 0:
        code, rule = "first_purchase_incentive", "no orders"
    elif rfm_label in RFM_ACTION:
        code, rule = RFM_ACTION[rfm_label], f"rfm segment = {rfm_label}"
        if band == "medium" and code in ("loyalty_reward", "cross_sell", "membership_upsell"):
            code, rule = "personalized_recommendation", f"rfm segment = {rfm_label} and churn >= {MEDIUM_CHURN}"
    else:
        code, rule = "nurture_newsletter", "default"
    action = ACTIONS[code]
    return {"action": action.code, "title": action.title, "channel": action.channel, "priority": action.priority,
            "description": action.description, "rule": rule, "signals": signals}
