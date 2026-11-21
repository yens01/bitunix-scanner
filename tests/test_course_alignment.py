"""
依《交易基礎教材｜重新整理版》對齊的回歸測試：
- BOS 後回測期間仍算 BOS（03 步不會在回測時消失）
- 壓力被突破後轉支撐（支撐壓力轉換）
- 目標取對向結構位，RR 不夠就是不夠，不把目標硬推過壓力
- MA 只是輔助，不會推翻清楚的 HH/HL 結構
"""

import random
import unittest

from sr_logic import (
    Candle,
    Zone,
    build_zones,
    atr,
    compute_targets,
    detect_bos_choch,
    detect_structure,
    evaluate_setup,
)

BULL_MTF = {"1H": "偏多", "4H": "偏多", "1D": "偏多"}
BEAR_MTF = {"1H": "偏空", "4H": "偏空", "1D": "偏空"}


def _path(seed: int = 7) -> list[Candle]:
    """盤整 → 下跌 → 上漲做出前高 105 → 回檔 HL → 突破 105（BOS）到 112 → 回踩 105。"""
    random.seed(seed)
    out: list[Candle] = []
    p, t = 100.0, 0
    for to, bars in [(100, 20), (95, 15), (105, 20), (100, 15), (112, 25), (105.3, 15)]:
        f = p
        for i in range(1, bars + 1):
            c = f + (to - f) * i / bars + (random.random() - 0.5) * 0.15
            out.append(
                Candle(t, p, max(p, c) + random.random() * 0.12, min(p, c) - random.random() * 0.12, c, 100)
            )
            p = c
            t += 900
    return out


def _retest_long() -> list[Candle]:
    base = _path()
    # 回踩突破位，下影掃過後收回區內（收針）
    return base + [Candle(base[-1].time + 900, 105.15, 105.3, 104.75, 105.22, 200)]


def _mirror(cs: list[Candle]) -> list[Candle]:
    return [Candle(c.time, 200 - c.open, 200 - c.low, 200 - c.high, 200 - c.close, c.volume) for c in cs]


def _step(r, num: str) -> bool:
    return next(ok for name, ok, _ in r.workflow if name.startswith(num))


class CourseAlignmentTest(unittest.TestCase):
    def test_bos_survives_retest(self):
        tag, score = detect_bos_choch(_retest_long())
        self.assertEqual(tag, "BOS向上")
        self.assertGreater(score, 0)

    def test_broken_resistance_becomes_support(self):
        cs = _retest_long()
        supports, _ = build_zones(cs, atr(cs, 14))
        self.assertTrue(any(z.lo <= 105.1 <= z.hi + 0.2 for z in supports), supports)

    def test_target_not_pushed_past_resistance(self):
        tp, rr = compute_targets("long", 100.0, 99.0, [], [Zone("resistance", 100.8, 101.2, 1, 1)], 1.0)
        self.assertAlmostEqual(tp, 100.8)
        self.assertAlmostEqual(rr, 0.8, places=6)
        tp, rr = compute_targets("short", 100.0, 101.0, [Zone("support", 98.8, 99.2, 1, 1)], [], 1.0)
        self.assertAlmostEqual(tp, 99.2)
        self.assertAlmostEqual(rr, 0.8, places=6)

    def test_ma_is_auxiliary(self):
        structure, detail, _, _ = detect_structure(_retest_long())
        self.assertEqual(structure, "偏多")
        self.assertNotIn("多空訊號打架", detail)

    def test_retest_long_passes_course_steps(self):
        r = evaluate_setup("TEST", _retest_long(), "15m", mtf=BULL_MTF)
        self.assertEqual(r.side, "long")
        for num in ("03", "04", "08", "09", "10"):
            self.assertTrue(_step(r, num), f"{num} 應通過：{r.workflow}")
        self.assertLess(r.stop, r.entry)
        self.assertGreater(r.tp, r.entry)
        self.assertAlmostEqual(r.rr, (r.tp - r.entry) / (r.entry - r.stop), places=2)

    def test_retest_short_mirror(self):
        r = evaluate_setup("TEST", _mirror(_retest_long()), "15m", mtf=BEAR_MTF)
        self.assertEqual(r.side, "short")
        for num in ("03", "08", "09", "10"):
            self.assertTrue(_step(r, num), f"{num} 應通過：{r.workflow}")
        self.assertGreater(r.stop, r.entry)
        self.assertLess(r.tp, r.entry)


if __name__ == "__main__":
    unittest.main()
