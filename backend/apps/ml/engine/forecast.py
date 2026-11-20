"""Time-series forecasting (ML_ARCHITECTURE §4/§6): seasonal-naive baseline, exponential smoothing (statsmodels
ETSModel, which gives prediction intervals) and SARIMAX. Univariate: the target is aggregated per period of the
time column. Evaluation is time-ordered (the last `horizon` periods are held out); the registered model is refit
on the full history and forecasts `horizon` periods ahead with 95% intervals.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .frames import coerce_series

FORECAST_ALGORITHMS = ("seasonal_naive", "ets", "sarimax")
SEASON_BY_FREQ = {"D": 7, "W": 52, "MS": 12, "QS": 4, "YS": 1, "h": 24}
MIN_PERIODS = 8


def infer_freq(timestamps: pd.Series) -> str:
    """Pandas offset alias from the median spacing of the distinct timestamps."""
    ts = pd.Series(pd.to_datetime(timestamps.dropna().unique())).sort_values()
    if len(ts) < 2:
        raise ValueError("At least two distinct timestamps are needed to infer a frequency.")
    step = ts.diff().dropna().median()
    days = step / pd.Timedelta(days=1)
    if days < 0.9:
        return "h"
    if days <= 1.5:
        return "D"
    if days <= 8:
        return "W"
    if days <= 35:
        return "MS"
    if days <= 100:
        return "QS"
    return "YS"


def prepare_series(df: pd.DataFrame, time_column: str, target: str, *, agg: str = "sum",
                   freq: str | None = None) -> tuple[pd.Series, dict]:
    """(series indexed by period start with a fixed frequency, info). Empty periods are 0 for `sum`, interpolated
    for `mean`."""
    ts = coerce_series(df[time_column], "datetime")
    y = coerce_series(df[target], "numeric")
    data = pd.DataFrame({"t": ts, "y": y}).dropna()
    if data.empty:
        raise ValueError("No rows with both a timestamp and a numeric target.")
    freq = freq or infer_freq(data["t"])
    grouped = data.set_index("t")["y"].resample(freq)
    series = grouped.sum() if agg == "sum" else grouped.mean()
    observed = data.set_index("t")["y"].resample(freq).count()
    gaps = int((observed == 0).sum())
    if agg != "sum":
        series = series.interpolate(limit_direction="both")
    series = series.astype("float64")
    series.index.freq = pd.tseries.frequencies.to_offset(freq)
    return series, {"frequency": freq, "periods": int(len(series)), "gaps": gaps, "aggregation": agg,
                    "start": series.index.min().isoformat(), "end": series.index.max().isoformat()}


def season_length(freq: str, n: int) -> int:
    m = SEASON_BY_FREQ.get(freq, 1)
    return m if m > 1 and n >= 2 * m + 2 else 1


# ------------------------------------------------------------------------------------------- forecasters
class SeasonalNaive:
    """y(t+h) = y(t+h-m) of the last observed season; intervals from the in-sample seasonal-difference spread."""

    name = "seasonal_naive"

    def __init__(self, season: int = 1):
        self.season = max(int(season), 1)

    def fit(self, y: np.ndarray) -> SeasonalNaive:
        y = np.asarray(y, dtype=float)
        m = self.season if len(y) > self.season else 1
        self.season = m
        self.last_ = y[-m:].copy()
        diffs = y[m:] - y[:-m] if len(y) > m else np.array([0.0])
        self.sigma_ = float(np.std(diffs, ddof=1)) if len(diffs) > 1 else 0.0
        return self

    def forecast(self, steps: int, alpha: float = 0.05) -> pd.DataFrame:
        from scipy.stats import norm

        z = float(norm.ppf(1 - alpha / 2))
        idx = np.arange(steps)
        mean = self.last_[idx % self.season]
        k = np.floor(idx / self.season) + 1
        half = z * self.sigma_ * np.sqrt(k)
        return pd.DataFrame({"mean": mean, "lower": mean - half, "upper": mean + half})


class ETSForecaster:
    """Additive-error exponential smoothing (statsmodels ETSModel): damped additive trend, additive season."""

    name = "ets"

    def __init__(self, season: int = 1, trend: bool = True, damped: bool = True):
        self.season, self.trend, self.damped = int(season), bool(trend), bool(damped)

    def fit(self, y: np.ndarray) -> ETSForecaster:
        from statsmodels.tsa.exponential_smoothing.ets import ETSModel

        y = pd.Series(np.asarray(y, dtype=float))  # ETSModel's prediction results need an indexed endog
        seasonal = "add" if self.season > 1 else None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = ETSModel(y, error="add", trend="add" if self.trend else None,
                             damped_trend=self.damped if self.trend else False, seasonal=seasonal,
                             seasonal_periods=self.season if seasonal else None)
            self.result_ = model.fit(disp=False, maxiter=500)
        self.n_ = len(y)
        return self

    def forecast(self, steps: int, alpha: float = 0.05) -> pd.DataFrame:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = self.result_.get_prediction(start=self.n_, end=self.n_ + steps - 1).summary_frame(alpha=alpha)
        return pd.DataFrame({"mean": frame["mean"].to_numpy(), "lower": frame["pi_lower"].to_numpy(),
                             "upper": frame["pi_upper"].to_numpy()})


class SARIMAXForecaster:
    name = "sarimax"

    def __init__(self, season: int = 1, order: tuple = (1, 1, 1), seasonal_order: tuple | None = None):
        self.season = int(season)
        self.order = tuple(order)
        self.seasonal_order = tuple(seasonal_order) if seasonal_order else (
            (1, 0, 1, self.season) if self.season > 1 else (0, 0, 0, 0))

    def fit(self, y: np.ndarray) -> SARIMAXForecaster:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = SARIMAX(np.asarray(y, dtype=float), order=self.order, seasonal_order=self.seasonal_order,
                            enforce_stationarity=False, enforce_invertibility=False)
            self.result_ = model.fit(disp=False, maxiter=200)
        return self

    def forecast(self, steps: int, alpha: float = 0.05) -> pd.DataFrame:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = self.result_.get_forecast(steps=steps).summary_frame(alpha=alpha)
        return pd.DataFrame({"mean": frame["mean"].to_numpy(), "lower": frame["mean_ci_lower"].to_numpy(),
                             "upper": frame["mean_ci_upper"].to_numpy()})


def make_forecaster(algorithm: str, season: int, params: dict | None = None):
    params = params or {}
    if algorithm == "seasonal_naive":
        return SeasonalNaive(season)
    if algorithm == "ets":
        return ETSForecaster(season, trend=params.get("trend", True), damped=params.get("damped", True))
    if algorithm == "sarimax":
        order = tuple(params.get("order", (1, 1, 1)))
        seasonal_order = params.get("seasonal_order")
        return SARIMAXForecaster(season, order=order, seasonal_order=tuple(seasonal_order) if seasonal_order else None)
    raise ValueError(f"Unknown forecast algorithm {algorithm!r}.")


# ------------------------------------------------------------------------------------------- metrics
def forecast_errors(actual: np.ndarray, predicted: np.ndarray) -> dict:
    a = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    err = a - p
    nonzero = np.abs(a) > 1e-9
    mape = float(np.mean(np.abs(err[nonzero] / a[nonzero])) * 100) if nonzero.any() else None
    denom = np.abs(a) + np.abs(p)
    ok = denom > 1e-9
    smape = float(np.mean(2 * np.abs(err[ok]) / denom[ok]) * 100) if ok.any() else None
    return {"mae": float(np.mean(np.abs(err))), "rmse": float(math.sqrt(np.mean(err ** 2))), "mape": mape,
            "smape": smape}


@dataclass
class ForecastModel:
    """The registered forecast artifact: the forecaster refit on the full history plus its calendar."""

    algorithm: str
    frequency: str
    season: int
    aggregation: str
    last_period: str
    forecaster: Any
    history_tail: list[float] = field(default_factory=list)

    def future_index(self, steps: int) -> pd.DatetimeIndex:
        start = pd.Timestamp(self.last_period)
        return pd.date_range(start, periods=steps + 1, freq=self.frequency)[1:]

    def forecast(self, steps: int, alpha: float = 0.05) -> pd.DataFrame:
        steps = max(int(steps), 1)
        frame = self.forecaster.forecast(steps, alpha=alpha)
        frame.insert(0, "period", [ts.isoformat() for ts in self.future_index(steps)])
        return frame

    def forecast_at(self, timestamps: list, alpha: float = 0.05) -> pd.DataFrame:
        """Forecasts for given future timestamps (each mapped to its period)."""
        last = pd.Timestamp(self.last_period)
        offset = pd.tseries.frequencies.to_offset(self.frequency)
        steps_for = []
        for t in timestamps:
            ts = pd.Timestamp(t)
            if ts <= last:
                raise ValueError(f"{ts.date()} is not after the end of the training history ({last.date()}).")
            idx = pd.date_range(last, ts + offset, freq=self.frequency)
            steps_for.append(max(int((idx <= ts).sum()) - 1, 1))
        full = self.forecast(max(steps_for))
        return full.iloc[[s - 1 for s in steps_for]].reset_index(drop=True)


def train_forecast(series: pd.Series, info: dict, *, algorithm: str, params: dict | None = None,
                   horizon: int | None = None) -> dict:
    """Holdout evaluation (last `horizon` periods) vs the seasonal-naive baseline, then a refit on everything."""
    n = len(series)
    if n < MIN_PERIODS:
        raise ValueError(f"At least {MIN_PERIODS} periods are needed to forecast (got {n}).")
    freq = info["frequency"]
    m = season_length(freq, n)
    h = int(horizon or max(min(m if m > 1 else 4, n // 4), 1))
    h = max(1, min(h, n // 3))
    y = series.to_numpy(dtype=float)
    train, test = y[:-h], y[-h:]
    m_train = season_length(freq, len(train))
    model = make_forecaster(algorithm, m_train, params).fit(train)
    pred = model.forecast(h)
    baseline = SeasonalNaive(m_train).fit(train).forecast(h)
    errs = forecast_errors(test, pred["mean"].to_numpy())
    base = forecast_errors(test, baseline["mean"].to_numpy())
    covered = float(np.mean((test >= pred["lower"].to_numpy()) & (test <= pred["upper"].to_numpy())))
    metrics = {**errs, "baseline_mae": base["mae"], "baseline_rmse": base["rmse"], "baseline_mape": base["mape"],
               "mae_vs_baseline": (errs["mae"] / base["mae"]) if base["mae"] else None,
               "interval_coverage": covered, "horizon": h, "season_length": m_train, "train_periods": len(train)}
    final = make_forecaster(algorithm, m, params).fit(y)
    fitted = ForecastModel(algorithm=algorithm, frequency=freq, season=m, aggregation=info.get("aggregation", "sum"),
                           last_period=series.index[-1].isoformat(), forecaster=final,
                           history_tail=[float(v) for v in y[-60:]])
    future = fitted.forecast(h)
    idx = series.index
    curves = {
        "history": [{"period": t.isoformat(), "value": float(v)} for t, v in zip(idx[-200:], y[-200:], strict=True)],
        "holdout": [{"period": t.isoformat(), "actual": float(a), "forecast": float(p), "lower": float(lo),
                     "upper": float(up), "baseline": float(b)}
                    for t, a, p, lo, up, b in zip(idx[-h:], test, pred["mean"], pred["lower"], pred["upper"],
                                                  baseline["mean"], strict=True)],
        "future": future.to_dict(orient="records"),
    }
    return {"model": fitted, "metrics": metrics, "curves": curves, "info": {**info, "season_length": m}}
