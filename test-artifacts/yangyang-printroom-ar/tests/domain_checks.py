"""木版版房晨检湿度预报的领域验收测试。

在批次根目录执行：

    export PYTHONPATH="$PWD/projects/yangyang-printroom-ar/backend${PYTHONPATH:+:$PYTHONPATH}"
    python3 test-artifacts/yangyang-printroom-ar/tests/domain_checks.py

用例名与 BUSINESS.md「值班剧本和验收步骤」一致，可附完整用例名运行单项：

    python3 test-artifacts/yangyang-printroom-ar/tests/domain_checks.py DomainChecks.test_input_validation
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date

BACKEND_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"),
)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from src.domain.checkpoint import InMemoryCheckpointStore, JsonFileCheckpointStore
from src.domain.fixtures import demo_rows
from src.domain.inputs import normalize_rows
from src.domain.model import predict_level, DifferenceAR
from src.domain.project import analyze
from src.utils.csv_files import read_rows, write_rows


DEFAULT_SETTINGS = {}


class DomainChecks(unittest.TestCase):
    def setUp(self):
        self.rows = demo_rows()
        self.store = InMemoryCheckpointStore()

    def _table(self, result, key):
        return next(table for table in result["tables"] if table["key"] == key)

    # 07:50 确定性种子、倒序导入与导出
    def test_deterministic_seed_csv_import_and_export(self):
        self.assertEqual(len(self.rows), 122)
        self.assertEqual(self.rows[0]["date"], "2026-07-20")
        self.assertEqual(self.rows[-1]["date"], "2026-11-24")
        self.assertEqual(self.rows[-1]["id"], "BF-20261124")
        self.assertAlmostEqual(self.rows[-1]["humidity_pct"], 74.69)
        recheck = [row for row in self.rows if row["reading_type"] == "同口径补测"]
        self.assertEqual(len(recheck), 4)

        columns = ["id", "date", "humidity_pct", "reading_type"]
        csv_text = write_rows(columns, list(reversed(self.rows)))
        imported = normalize_rows(read_rows(csv_text, columns))
        self.assertEqual(imported, normalize_rows(self.rows))

        result_a = analyze(self.rows, DEFAULT_SETTINGS, InMemoryCheckpointStore())
        result_b = analyze(imported, DEFAULT_SETTINGS, InMemoryCheckpointStore())
        self.assertEqual(
            json.dumps(result_a, ensure_ascii=False, sort_keys=True),
            json.dumps(result_b, ensure_ascii=False, sort_keys=True),
        )

        # 缺测与未计算单元格导出为空字符串
        tail_gap = [row for row in self.rows if row["date"] != "2026-11-23"]
        gapped = analyze(tail_gap, DEFAULT_SETTINGS, InMemoryCheckpointStore())
        future = self._table(gapped, "future_checks")
        exported = write_rows(future["columns"], future["rows"]).splitlines()
        self.assertEqual(len(exported), len(future["rows"]) + 1)
        for row in future["rows"]:
            self.assertIsNone(row["anchor_humidity"])
            self.assertIsNone(row["raw_humidity"])

    # 07:54 输入校验
    def test_input_validation(self):
        def reject(rows, fragment):
            with self.assertRaises(ValueError) as context:
                normalize_rows(rows)
            self.assertIn(fragment, str(context.exception))

        bad_date = [dict(row, date="2026-02-30") for row in self.rows]
        reject(bad_date, "日期")

        duplicate = [dict(row) for row in self.rows]
        duplicate[1] = {**duplicate[0]}
        reject(duplicate, "重复")

        too_wet = [dict(row) for row in self.rows]
        too_wet[0] = {**too_wet[0], "humidity_pct": 100.01}
        reject(too_wet, "0 至 100")

        # 0 与 100 保持有效
        boundary = [dict(row) for row in self.rows]
        boundary[0] = {**boundary[0], "humidity_pct": 0}
        boundary[1] = {**boundary[1], "humidity_pct": 100}
        normalized = normalize_rows(boundary)
        self.assertEqual(normalized[0]["humidity_pct"], 0.0)
        self.assertEqual(normalized[1]["humidity_pct"], 100.0)

    # 08:00 / 08:09 缺测切断滞后链、共用评分日、断点续跑
    def test_gap_resets_lags_and_usability_is_shared_by_both_scores(self):
        result = analyze(self.rows, DEFAULT_SETTINGS, self.store)
        metrics = result["metrics"]
        self.assertEqual(metrics["回测评分天数"], 21)
        self.assertAlmostEqual(metrics["差分模型 MAE（百分点）"], 0.503, places=3)
        self.assertAlmostEqual(metrics["沿用昨日 MAE（百分点）"], 1.795, places=3)
        self.assertAlmostEqual(metrics["MAE 改善（百分点）"], 1.292, places=3)
        self.assertEqual(metrics["台账缺测天数"], 6)

        backtest = self._table(result, "rolling_backtest")
        statuses = {row["date"]: row["status"] for row in backtest["rows"]}
        # 缺口后两天滞后链仍受影响，11 月 2 日恢复评分
        self.assertEqual(statuses["2026-10-30"], "滞后链中断")
        self.assertEqual(statuses["2026-10-31"], "滞后链中断")
        self.assertEqual(statuses["2026-11-02"], "已评分")
        # 缺测日标明缺少实测
        for missing in ("2026-10-28", "2026-10-29", "2026-11-12"):
            self.assertEqual(statuses[missing], "缺少实测")
        # 模型与基线只在相同的已评分日期计算
        scored = [row for row in backtest["rows"] if row["status"] == "已评分"]
        self.assertTrue(all(row["absolute_error"] is not None for row in scored))
        self.assertTrue(all(row["baseline_absolute_error"] is not None for row in scored))
        skipped = [row for row in backtest["rows"] if row["status"] != "已评分"]
        self.assertTrue(all(row["absolute_error"] is None for row in skipped))

        # 尾段缺口：未来日期仍生成但预测留空，缺测表优先列出
        tail_gap = [row for row in self.rows if row["date"] != "2026-11-23"]
        gapped = analyze(tail_gap, DEFAULT_SETTINGS, InMemoryCheckpointStore())
        future = self._table(gapped, "future_checks")
        self.assertEqual(len(future["rows"]), 5)
        self.assertTrue(all(row["predicted_humidity"] is None for row in future["rows"]))
        gaps = self._table(gapped, "missing_periods")
        self.assertEqual(gaps["rows"][0]["from_date"], "2026-11-23")
        self.assertEqual(gaps["rows"][0]["priority"], "影响本次预报")

        # 断点续跑：文件存储中断后恢复，判过的日期不重算
        with tempfile.TemporaryDirectory() as directory:
            file_store = JsonFileCheckpointStore(directory)
            first = analyze(self.rows, DEFAULT_SETTINGS, file_store)
            self.assertEqual(first["metrics"]["本次新判天数"], 30)
            self.assertEqual(first["metrics"]["断点续跑恢复天数"], 0)
            checkpoint = next(
                name for name in os.listdir(directory) if name.startswith("backtest-")
            )
            with open(os.path.join(directory, checkpoint), encoding="utf-8") as handle:
                entries = json.load(handle)
            with open(os.path.join(directory, checkpoint), "w", encoding="utf-8") as handle:
                json.dump(entries[:10], handle, ensure_ascii=False)
            resumed = analyze(self.rows, DEFAULT_SETTINGS, JsonFileCheckpointStore(directory))
            self.assertEqual(resumed["metrics"]["断点续跑恢复天数"], 10)
            self.assertEqual(resumed["metrics"]["本次新判天数"], 20)
            self.assertEqual(
                self._table(resumed, "rolling_backtest")["rows"],
                self._table(first, "rolling_backtest")["rows"],
            )

    # 08:05 滚动拟合只用目标日之前的样本
    def test_rolling_fit_uses_only_records_available_before_target(self):
        target = "2026-11-10"
        baseline = analyze(self.rows, {"lag_order": 1}, InMemoryCheckpointStore())
        altered = [dict(row) for row in self.rows]
        for row in altered:
            if row["date"] >= target:
                row["humidity_pct"] = min(100.0, row["humidity_pct"] + 1.5)
        changed = analyze(altered, {"lag_order": 1}, InMemoryCheckpointStore())

        base_rows = {r["date"]: r for r in self._table(baseline, "rolling_backtest")["rows"]}
        changed_rows = {r["date"]: r for r in self._table(changed, "rolling_backtest")["rows"]}
        # 目标日及之后的预测，目标日之前的保持一致
        for day, row in base_rows.items():
            if day < target:
                self.assertEqual(row["predicted_humidity"], changed_rows[day]["predicted_humidity"])
                self.assertEqual(row["fit_through"], changed_rows[day]["fit_through"])
        # 目标日实测改变，误差随之改变
        self.assertNotEqual(
            base_rows[target]["absolute_error"], changed_rows[target]["absolute_error"],
        )
        self.assertEqual(changed_rows[target]["fit_through"], "2026-11-09")

    # 08:09 限幅与限幅后变化递推
    def test_clipping_and_recursion_use_the_applied_difference(self):
        # 上界：锚 99、还原 101.5 时当日 100，递推差分 +1
        model = DifferenceAR(intercept=2.5, weights=(0.0,))
        upper = predict_level(model, 99.0, (0.0,))
        self.assertAlmostEqual(upper.raw_humidity, 101.5)
        self.assertEqual(upper.humidity, 100.0)
        self.assertEqual(upper.applied_change, 1.0)
        self.assertEqual(upper.boundary, "触发上界")

        lower = predict_level(DifferenceAR(intercept=-3.0, weights=(0.0,)), 2.0, (0.0,))
        self.assertEqual(lower.humidity, 0.0)
        self.assertEqual(lower.applied_change, -2.0)
        self.assertEqual(lower.boundary, "触发下界")

        # 完整递推链：触发上界后下一天锚值为 100，输入差分为限幅后的实际变化
        hike = [dict(row) for row in self.rows]
        for row in hike[-6:]:
            row["humidity_pct"] = min(100.0, row["humidity_pct"] + 24.0)
        result = analyze(hike, {"forecast_days": 6}, InMemoryCheckpointStore())
        future = self._table(result, "future_checks")["rows"]
        clipped = next(row for row in future if row["boundary"] == "触发上界")
        index = future.index(clipped)
        self.assertEqual(future[index + 1]["anchor_humidity"], 100.0)
        self.assertEqual(clipped["applied_change"], round(100.0 - clipped["anchor_humidity"], 3))
        for earlier, later in zip(future, future[1:]):
            self.assertEqual(later["anchor_humidity"], earlier["predicted_humidity"])

    # 08:14 参数影响与日历日期；08:20 相邻连湿清单
    def test_parameter_effects_and_calendar_dates(self):
        result = analyze(
            self.rows, {"wet_threshold": 72, "forecast_days": 14}, InMemoryCheckpointStore(),
        )
        future = self._table(result, "future_checks")
        self.assertEqual(future["rows"][-1]["date"], "2026-12-08")
        actions = {row["date"]: row["action"] for row in future["rows"]}
        self.assertEqual(actions["2026-11-25"], "优先晨检")
        self.assertEqual(actions["2026-11-26"], "优先晨检")
        for day in ("2026-11-27", "2026-11-28", "2026-11-29"):
            self.assertEqual(actions[day], "常规晨检")
        # 回测指标与默认运行一致
        default = analyze(self.rows, DEFAULT_SETTINGS, InMemoryCheckpointStore())
        for key in ("回测评分天数", "差分模型 MAE（百分点）", "沿用昨日 MAE（百分点）", "MAE 改善（百分点）"):
            self.assertEqual(result["metrics"][key], default["metrics"][key])

        # 默认阈值 65：相邻两天都达阈值才列入连湿，且共用同一组起算读数
        wet = self._table(default, "adjacent_wet_pairs")
        self.assertTrue(wet["rows"])
        for row in wet["rows"]:
            self.assertGreaterEqual(row["first_humidity"], 65)
            self.assertGreaterEqual(row["second_humidity"], 65)
            self.assertEqual(row["anchor_origin"], "2026-11-24")
            first = date.fromisoformat(row["first_date"])
            second = date.fromisoformat(row["second_date"])
            self.assertEqual((second - first).days, 1)
        # 未来清单中的连湿标记与连湿表日期集合一致
        marked = {row["date"] for row in self._table(default, "future_checks")["rows"]
                  if row["in_wet_pair"] == "是"}
        paired = set()
        for row in wet["rows"]:
            paired.update((row["first_date"], row["second_date"]))
        self.assertEqual(marked, paired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
