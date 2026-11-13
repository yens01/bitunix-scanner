import unittest
from datetime import datetime, timezone
from sr_logic import Candle, build_day_range_box


def candle(iso, high, low):
    t = int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return Candle(time=t, open=low, high=high, low=low, close=high, volume=1)


class Taiwan0800TradingDayTests(unittest.TestCase):
    def test_boundary_0800_taipei(self):
        # Taiwan 07:45 is previous day, 08:00 belongs to new trading day.
        cs = [
            candle("2026-11-12T23:45:00", 999, 100),
            candle("2026-11-13T00:00:00", 200, 180),
            candle("2026-11-13T00:15:00", 210, 175),
            candle("2026-11-13T00:30:00", 220, 170),
            candle("2026-11-13T00:45:00", 230, 160),
        ]
        box = build_day_range_box(cs, day="2026-11-13", tz_offset_hours=8,
                                  day_start_hour=8)
        self.assertEqual((box.lo, box.hi), (160, 230))

    def test_early_day_does_not_fall_back_to_previous_24h(self):
        cs = [
            candle("2026-11-12T23:00:00", 999, 100),
            candle("2026-11-12T23:15:00", 900, 110),
            candle("2026-11-12T23:30:00", 800, 120),
            candle("2026-11-13T00:00:00", 200, 180),
        ]
        self.assertIsNone(build_day_range_box(cs, day="2026-11-13",
                                              day_start_hour=8))

    def test_before_0800_is_previous_trading_day(self):
        cs = [
            candle("2026-11-12T00:00:00", 300, 250),
            candle("2026-11-12T00:15:00", 310, 240),
            candle("2026-11-12T23:30:00", 320, 230),
            candle("2026-11-12T23:45:00", 330, 220),
        ]
        box = build_day_range_box(cs, day_start_hour=8)
        self.assertEqual((box.lo, box.hi), (220, 330))


if __name__ == "__main__":
    unittest.main()
