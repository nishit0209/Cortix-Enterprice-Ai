"""Next-best-action rules (pure)."""

import pytest

from apps.customers import nba


@pytest.mark.parametrize(("kwargs", "action"), [
    ({"rfm_label": "Champions", "order_count": 9, "ticket_count": 4, "churn_probability": 0.7}, "service_recovery"),
    ({"rfm_label": "Champions", "order_count": 9, "churn_probability": 0.8, "clv_percentile": 0.9},
     "vip_retention_offer"),
    ({"rfm_label": "Loyal customers", "order_count": 5, "churn_probability": 0.65}, "win_back_campaign"),
    ({"rfm_label": None, "order_count": 0}, "first_purchase_incentive"),
    ({"rfm_label": "Champions", "order_count": 9, "churn_probability": 0.1}, "loyalty_reward"),
    ({"rfm_label": "Champions", "order_count": 9, "churn_probability": 0.4}, "personalized_recommendation"),
    ({"rfm_label": "Loyal customers", "order_count": 4}, "cross_sell"),
    ({"rfm_label": "New customers", "order_count": 1}, "onboarding_nurture"),
    ({"rfm_label": "At risk", "order_count": 3}, "reactivation_offer"),
    ({"rfm_label": "Hibernating", "order_count": 1}, "low_cost_reengagement"),
    ({"rfm_label": None, "order_count": 2}, "nurture_newsletter"),
])
def test_rules(kwargs, action):
    out = nba.decide(**kwargs)
    assert out["action"] == action
    assert out["title"] and out["priority"] in ("high", "medium", "low") and out["rule"]
    assert out["signals"]["order_count"] == kwargs["order_count"]


def test_every_rfm_label_has_an_action():
    from apps.customers.matching import RFM_LABELS

    assert set(RFM_LABELS) <= set(nba.RFM_ACTION)


def test_churn_band_and_percentiles():
    assert nba.churn_band(None) is None
    assert [nba.churn_band(p) for p in (0.1, 0.4, 0.9)] == ["low", "medium", "high"]
    assert nba.value_percentiles({"a": 10.0, "b": 20.0, "c": 30.0}) == {"a": 0.0, "b": 0.5, "c": 1.0}
    assert nba.value_percentiles({"a": 5.0}) == {"a": 1.0}
    assert nba.value_percentiles({}) == {}
