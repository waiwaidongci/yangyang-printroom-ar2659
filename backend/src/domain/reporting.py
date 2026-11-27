"""将湿度、误差、缺测和模型系数整理成可导出的晨报。"""

from datetime import date
from statistics import mean

from .evaluation import BacktestDay, ForecastDay, ResumeStats
from .inputs import MissingRange
from .model import DifferenceAR, Prediction


def iso_day(day: int) -> str:
    return date.fromordinal(day).isoformat()


def measurement(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def report_table(key: str, title: str, labels: dict[str, str], rows: list[dict]) -> dict:
    return {"key": key, "title": title, "columns": list(labels), "labels": labels, "rows": rows}


def prediction_cells(prediction: Prediction | None) -> dict:
    if prediction is None:
        return {"raw_humidity": None, "predicted_humidity": None, "boundary": None}
    return {
        "raw_humidity": measurement(prediction.raw_humidity),
        "predicted_humidity": measurement(prediction.humidity),
        "boundary": prediction.boundary,
    }


def backtest_report(
    backtest: list[BacktestDay], missing_days: int, resume: ResumeStats | None = None,
) -> tuple[dict, dict]:
    model_errors = []
    baseline_errors = []
    rows = []
    for result in backtest:
        model_error = baseline_error = None
        if result.prediction is not None:
            model_error = abs(result.prediction.humidity - result.actual)
            baseline_error = abs(result.persistence - result.actual)
            model_errors.append(model_error)
            baseline_errors.append(baseline_error)
        rows.append({
            "date": iso_day(result.day),
            "status": result.status,
            "fit_through": iso_day(result.day - 1) if result.prediction is not None else None,
            "training_pairs": result.sample_count,
            "actual_humidity": measurement(result.actual),
            **prediction_cells(result.prediction),
            "baseline_humidity": measurement(result.persistence),
            "absolute_error": measurement(model_error),
            "baseline_absolute_error": measurement(baseline_error),
        })

    model_mae = mean(model_errors) if model_errors else None
    baseline_mae = mean(baseline_errors) if baseline_errors else None
    improvement = baseline_mae - model_mae if model_errors else None
    metrics = {
        "回测评分天数": len(model_errors),
        "差分模型 MAE（百分点）": measurement(model_mae),
        "沿用昨日 MAE（百分点）": measurement(baseline_mae),
        "MAE 改善（百分点）": measurement(improvement),
        "台账缺测天数": missing_days,
        "断点续跑恢复天数": 0 if resume is None else resume.restored_days,
        "本次新判天数": len(backtest) if resume is None else resume.computed_days,
    }
    table = report_table("rolling_backtest", "逐日一步回测", {
        "date": "目标日",
        "status": "评分状态",
        "fit_through": "拟合截止日",
        "training_pairs": "目标日前可用样本数",
        "actual_humidity": "晨检实测（%RH）",
        "raw_humidity": "还原原值（%RH）",
        "predicted_humidity": "限幅预测（%RH）",
        "boundary": "物理边界",
        "baseline_humidity": "沿用昨日（%RH）",
        "absolute_error": "模型绝对误差（百分点）",
        "baseline_absolute_error": "基线绝对误差（百分点）",
    }, rows)
    return metrics, table


def wet_pairs(forecasts: list[ForecastDay], threshold: int) -> list[tuple[ForecastDay, ForecastDay]]:
    """预报期内相邻两个自然日都达到晨检阈值的连湿对。

    两天都由末次实测的同一组起算读数、同一次递推链推出；
    尾段缺测导致未生成预测的日期不参与连湿判定。
    """
    pairs = []
    for earlier, later in zip(forecasts, forecasts[1:]):
        if (
            earlier.prediction is not None
            and later.prediction is not None
            and earlier.prediction.humidity >= threshold
            and later.prediction.humidity >= threshold
        ):
            pairs.append((earlier, later))
    return pairs


def forecast_report(forecasts: list[ForecastDay], threshold: int) -> dict:
    wet_pair_days = set()
    for earlier, later in wet_pairs(forecasts, threshold):
        wet_pair_days.add(earlier.day)
        wet_pair_days.add(later.day)

    rows = []
    for horizon, result in enumerate(forecasts, start=1):
        prediction = result.prediction
        if prediction is None:
            action = "先核对近期台账或补齐连续晨检"
        elif prediction.boundary != "未触发":
            action = "优先实测，核查预测越界原因"
        elif prediction.humidity >= threshold:
            action = "优先晨检"
        else:
            action = "常规晨检"
        rows.append({
            "date": iso_day(result.day),
            "horizon": horizon,
            "status": "待补连续台账" if prediction is None else "已预报",
            "anchor_humidity": measurement(result.anchor),
            **prediction_cells(prediction),
            "applied_change": measurement(prediction.applied_change) if prediction is not None else None,
            "in_wet_pair": "是" if result.day in wet_pair_days else "否",
            "action": action,
        })
    return report_table("future_checks", "未来晨检清单", {
        "date": "晨检日期",
        "horizon": "距末次实测（日）",
        "status": "预报状态",
        "anchor_humidity": "前一日锚值（%RH）",
        "raw_humidity": "还原原值（%RH）",
        "predicted_humidity": "限幅预报（%RH）",
        "boundary": "物理边界",
        "applied_change": "向后递推的差分（百分点）",
        "in_wet_pair": "是否列入相邻连湿",
        "action": "交班安排",
    }, rows)


def adjacent_wet_report(
    forecasts: list[ForecastDay], threshold: int, last_measured_day: int,
) -> dict:
    """晨报清单：列全预报期内可能连续两天高湿、需要搬版的相邻日期。"""
    anchor_origin = iso_day(last_measured_day)
    rows = []
    for earlier, later in wet_pairs(forecasts, threshold):
        rows.append({
            "first_date": iso_day(earlier.day),
            "second_date": iso_day(later.day),
            "first_humidity": measurement(earlier.prediction.humidity),
            "second_humidity": measurement(later.prediction.humidity),
            "threshold": threshold,
            "anchor_origin": anchor_origin,
            "action": "连续两天达到晨检阈值，安排搬版并优先晨检。",
        })
    return report_table("adjacent_wet_pairs", "相邻连湿搬版清单", {
        "first_date": "连湿起日",
        "second_date": "连湿止日",
        "first_humidity": "起日限幅预报（%RH）",
        "second_humidity": "止日限幅预报（%RH）",
        "threshold": "晨检阈值（%RH）",
        "anchor_origin": "共同起算实测日",
        "action": "交班安排",
    }, rows)


def gaps_report(gaps: list[MissingRange], last: int, order: int, first_backtest: int) -> dict:
    rows = []
    for gap in reversed(gaps):
        if gap.last >= last - order:
            priority = "影响本次预报"
        elif gap.last >= first_backtest - order - 1:
            priority = "影响近期回测"
        else:
            priority = "历史台账留档"
        rows.append({
            "from_date": iso_day(gap.first),
            "through_date": iso_day(gap.last),
            "days": gap.days,
            "priority": priority,
            "action": "核对当日留存的同口径读数，确认没有记录时保留缺测。",
        })
    return report_table("missing_periods", "缺测回查清单", {
        "from_date": "缺测起日",
        "through_date": "缺测止日",
        "days": "缺测天数",
        "priority": "回查优先级",
        "action": "回查办法",
    }, rows)


def model_report(model: DifferenceAR, last: int, pair_count: int) -> dict:
    terms = ["截距", *(f"滞后 {lag} 日差分" for lag in range(1, len(model.weights) + 1))]
    rows = [
        {"term": term, "coefficient": round(coefficient, 6), "training_pairs": pair_count, "fit_through": iso_day(last)}
        for term, coefficient in zip(terms, (model.intercept, *model.weights), strict=True)
    ]
    return report_table("model_terms", "未来预报模型系数", {
        "term": "模型项",
        "coefficient": "系数",
        "training_pairs": "有效训练样本数",
        "fit_through": "拟合截止日",
    }, rows)
