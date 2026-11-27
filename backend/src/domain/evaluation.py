"""滚动评分与从最新实测读数开始的递推预报。"""

from bisect import bisect_left
from dataclasses import dataclass

from .checkpoint import CheckpointStore, InMemoryCheckpointStore, SavedBacktestDay, backtest_fingerprint
from .model import (
    DifferenceAR,
    Prediction,
    TrainingPair,
    fit_difference_ar,
    minimum_pairs,
    predict_level,
    trailing_differences,
)


@dataclass(frozen=True)
class BacktestDay:
    day: int
    sample_count: int
    status: str
    actual: float | None
    persistence: float | None = None
    prediction: Prediction | None = None


@dataclass(frozen=True)
class ForecastDay:
    day: int
    anchor: float | None
    prediction: Prediction | None


@dataclass(frozen=True)
class ResumeStats:
    restored_days: int
    computed_days: int
    resumed: bool


def evaluate_backtest_day(
    readings: dict[int, float],
    pairs: list[TrainingPair],
    pair_days: list[int],
    eligible: dict[int, TrainingPair],
    minimum: int,
    order: int,
    day: int,
) -> BacktestDay:
    """对单个回测自然日做一次完整判定，逻辑与断点存储无关。"""
    train_count = bisect_left(pair_days, day)
    actual = readings.get(day)
    pair = eligible.get(day)
    if actual is None:
        return BacktestDay(day, train_count, "缺少实测", None)
    if pair is None:
        return BacktestDay(day, train_count, "滞后链中断", actual)
    if train_count < minimum:
        return BacktestDay(day, train_count, "训练样本不足", actual)
    model = fit_difference_ar(pairs[:train_count], order)
    anchor = readings[day - 1]
    prediction = predict_level(model, anchor, pair.lags)
    return BacktestDay(day, train_count, "已评分", actual, anchor, prediction)


def _to_saved(result: BacktestDay) -> SavedBacktestDay:
    prediction = result.prediction
    return SavedBacktestDay(
        day=result.day,
        sample_count=result.sample_count,
        status=result.status,
        actual=result.actual,
        persistence=result.persistence,
        raw_humidity=None if prediction is None else prediction.raw_humidity,
        humidity=None if prediction is None else prediction.humidity,
        applied_change=None if prediction is None else prediction.applied_change,
        boundary=None if prediction is None else prediction.boundary,
    )


def _from_saved(entry: SavedBacktestDay) -> BacktestDay:
    if entry.humidity is None:
        prediction = None
    else:
        prediction = Prediction(entry.raw_humidity, entry.humidity, entry.applied_change, entry.boundary)
    return BacktestDay(
        day=entry.day,
        sample_count=entry.sample_count,
        status=entry.status,
        actual=entry.actual,
        persistence=entry.persistence,
        prediction=prediction,
    )


def resumable_rolling_backtest(
    readings: dict[int, float],
    pairs: list[TrainingPair],
    order: int,
    first: int,
    last: int,
    store: CheckpointStore | None = None,
) -> tuple[list[BacktestDay], ResumeStats]:
    """逐日滚动回测，已判定日期从断点取回，未判定日期才重新拟合。

    指纹覆盖台账读数、滞后阶数与回测窗口；输入变化会得到不同指纹，
    不会复用旧判定。每个自然日判定后立即落盘，中断后续跑无需重算。
    """
    store = InMemoryCheckpointStore() if store is None else store
    key = backtest_fingerprint(readings, order, first, last)
    saved = store.load(key)
    saved_by_day = {entry.day: entry for entry in saved if first <= entry.day <= last}

    pair_days = [pair.day for pair in pairs]
    eligible = {pair.day: pair for pair in pairs}
    minimum = minimum_pairs(order)

    restored = 0
    computed = 0
    entries: list[SavedBacktestDay] = []
    report: list[BacktestDay] = []
    for day in range(first, last + 1):
        cached = saved_by_day.get(day)
        if cached is not None:
            result = _from_saved(cached)
            restored += 1
        else:
            result = evaluate_backtest_day(
                readings, pairs, pair_days, eligible, minimum, order, day,
            )
            computed += 1
        entries.append(_to_saved(result))
        report.append(result)
        # 逐日落盘：即使在窗口中间中断，已判日期也能直接续跑。
        store.save(key, entries)

    stats = ResumeStats(restored_days=restored, computed_days=computed, resumed=bool(saved))
    return report, stats


def rolling_backtest(
    readings: dict[int, float], pairs: list[TrainingPair], order: int, first: int, last: int,
) -> list[BacktestDay]:
    """无断点的便捷入口，保持与既有调用兼容。"""
    report, _ = resumable_rolling_backtest(readings, pairs, order, first, last)
    return report


def forecast_future(
    readings: dict[int, float], model: DifferenceAR, last: int, order: int, days: int,
) -> list[ForecastDay]:
    lags = trailing_differences(readings, last, order)
    if lags is None:
        return [ForecastDay(last + step, None, None) for step in range(1, days + 1)]

    report = []
    anchor = readings[last]
    for step in range(1, days + 1):
        prediction = predict_level(model, anchor, lags)
        report.append(ForecastDay(last + step, anchor, prediction))
        # 先把还原值限幅到 0–100，再用限幅后的锚值与实际变化递推下一天。
        anchor = prediction.humidity
        lags = (prediction.applied_change, *lags[:-1])
    return report
