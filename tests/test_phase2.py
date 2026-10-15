"""
Phase 2 Tests: Time Handling, Timestamp Validation, and Business Rules (R1-R5, R8).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from fastapi import HTTPException

from app.main import (
    IST,
    MAX_EPOCH_MS,
    MIN_EPOCH_MS,
    UTC,
    TimestampValidationError,
    calculate_half_day,
    calculate_late_minutes,
    calculate_overtime_minutes,
    calculate_work_hours,
    epoch_ms_to_utc_datetime,
    get_attendance_date,
    get_shift_boundaries,
    round_half_up,
    to_ist,
    truncate_to_seconds,
    utc_datetime_to_epoch_ms,
    validate_epoch_ms,
)


class TestTimestampValidation(unittest.TestCase):
    def test_valid_epoch_ms_accepted(self):
        """Valid integer epoch milliseconds within bounds must be accepted."""
        valid_ts = 1783329000000
        self.assertEqual(validate_epoch_ms(valid_ts), valid_ts)
        self.assertEqual(validate_epoch_ms(MIN_EPOCH_MS), MIN_EPOCH_MS)
        self.assertEqual(validate_epoch_ms(MAX_EPOCH_MS), MAX_EPOCH_MS)

    def test_strings_rejected(self):
        """String timestamps must be rejected with 422."""
        with self.assertRaises((HTTPException, ValueError)) as ctx:
            validate_epoch_ms("1783329000000")
        if isinstance(ctx.exception, HTTPException):
            self.assertEqual(ctx.exception.status_code, 422)

    def test_floats_rejected(self):
        """Floating-point timestamps must be rejected with 422."""
        with self.assertRaises((HTTPException, ValueError)) as ctx:
            validate_epoch_ms(1783329000000.5)
        if isinstance(ctx.exception, HTTPException):
            self.assertEqual(ctx.exception.status_code, 422)

    def test_booleans_rejected(self):
        """Booleans (True/False) must be explicitly rejected despite being int subclass."""
        with self.assertRaises((HTTPException, ValueError)):
            validate_epoch_ms(True)
        with self.assertRaises((HTTPException, ValueError)):
            validate_epoch_ms(False)

    def test_epoch_seconds_rejected(self):
        """Values formatted as epoch seconds (< 100000000000) must be rejected with 422."""
        with self.assertRaises((HTTPException, ValueError)) as ctx:
            validate_epoch_ms(1783329000)
        if isinstance(ctx.exception, HTTPException):
            self.assertEqual(ctx.exception.status_code, 422)

    def test_out_of_range_boundaries_rejected(self):
        """Timestamps outside [MIN_EPOCH_MS, MAX_EPOCH_MS] must be rejected."""
        with self.assertRaises((HTTPException, ValueError)):
            validate_epoch_ms(MIN_EPOCH_MS - 1)
        with self.assertRaises((HTTPException, ValueError)):
            validate_epoch_ms(MAX_EPOCH_MS + 1)

    def test_none_when_required_rejected(self):
        """None must be rejected when required=True."""
        with self.assertRaises((HTTPException, ValueError)):
            validate_epoch_ms(None, required=True)

    def test_none_when_optional_allowed(self):
        """None must return None when required=False."""
        self.assertIsNone(validate_epoch_ms(None, required=False))


class TestTimeConversion(unittest.TestCase):
    def test_utc_to_ist_conversion(self):
        """UTC instant correctly converted to IST (+05:30)."""
        utc_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        ist_dt = to_ist(utc_dt)
        self.assertEqual(ist_dt.hour, 9)
        self.assertEqual(ist_dt.minute, 30)
        self.assertEqual(ist_dt.second, 0)
        self.assertEqual(ist_dt.utcoffset(), timedelta(hours=5, minutes=30))

    def test_epoch_ms_roundtrip(self):
        """Converting epoch ms -> datetime -> epoch ms preserves exact value."""
        original_ms = 1783329000000
        dt = epoch_ms_to_utc_datetime(original_ms)
        roundtrip_ms = utc_datetime_to_epoch_ms(dt)
        self.assertEqual(roundtrip_ms, original_ms)

    def test_fractional_seconds_truncated_not_rounded(self):
        """09:40:00.900 must truncate to 09:40:00 (discarding microsecond, not rounding)."""
        dt_with_micros = datetime(2026, 7, 6, 9, 40, 0, 900000, tzinfo=IST)
        truncated = truncate_to_seconds(dt_with_micros)
        self.assertEqual(truncated.microsecond, 0)
        self.assertEqual(truncated.second, 0)
        self.assertEqual(truncated.minute, 40)
        self.assertEqual(truncated.hour, 9)

    def test_naive_datetime_handled_safely(self):
        """Naive datetimes are assumed UTC and made timezone-aware."""
        naive_dt = datetime(2026, 7, 6, 4, 0, 0)
        truncated = truncate_to_seconds(naive_dt)
        self.assertIsNotNone(truncated.tzinfo)
        ist_dt = to_ist(naive_dt)
        self.assertEqual(ist_dt.hour, 9)
        self.assertEqual(ist_dt.minute, 30)


class TestAttendanceDateAndShiftBoundaries(unittest.TestCase):
    def test_normal_daytime_shift(self):
        """Normal shift (09:30 - 18:30) date is the IST calendar date of punch_in."""
        # 2026-07-06 03:58:00 UTC = 2026-07-06 09:28:00 IST
        punch_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
        att_date = get_attendance_date(punch_in, "09:30", "18:30")
        self.assertEqual(att_date, "2026-07-06")

    def test_overnight_punch_in_before_midnight(self):
        """Overnight shift (22:00 - 06:00), punch-in before midnight belongs to that date."""
        # 2026-07-06 16:25:00 UTC = 2026-07-06 21:55:00 IST
        punch_in = datetime(2026, 7, 6, 16, 25, 0, tzinfo=UTC)
        att_date = get_attendance_date(punch_in, "22:00", "06:00")
        self.assertEqual(att_date, "2026-07-06")

    def test_overnight_punch_in_after_midnight_before_shift_end(self):
        """Overnight shift (22:00 - 06:00), punch-in at 01:15 IST belongs to previous day."""
        # 2026-07-06 19:45:00 UTC = 2026-07-07 01:15:00 IST
        punch_in = datetime(2026, 7, 6, 19, 45, 0, tzinfo=UTC)
        att_date = get_attendance_date(punch_in, "22:00", "06:00")
        self.assertEqual(att_date, "2026-07-06")

    def test_overnight_exact_shift_end_boundary(self):
        """Overnight shift: punch-in at exactly 06:00:00 IST is NOT earlier than shift_end."""
        # Punch-in at 06:00:00 IST on 2026-07-07 belongs to 2026-07-07
        punch_in = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
        att_date = get_attendance_date(punch_in, "22:00", "06:00")
        self.assertEqual(att_date, "2026-07-07")

    def test_overnight_one_second_before_shift_end(self):
        """Overnight shift: punch-in at 05:59:59 IST is strictly earlier than 06:00:00."""
        punch_in = datetime(2026, 7, 7, 5, 59, 59, tzinfo=IST)
        att_date = get_attendance_date(punch_in, "22:00", "06:00")
        self.assertEqual(att_date, "2026-07-06")

    def test_shift_boundaries_normal_and_overnight(self):
        """Verify get_shift_boundaries correctly calculates start and end."""
        # Normal shift
        start, end = get_shift_boundaries("2026-07-06", "09:30", "18:30")
        self.assertEqual(start, datetime(2026, 7, 6, 9, 30, tzinfo=IST))
        self.assertEqual(end, datetime(2026, 7, 6, 18, 30, tzinfo=IST))

        # Overnight shift
        start_on, end_on = get_shift_boundaries("2026-07-06", "22:00", "06:00")
        self.assertEqual(start_on, datetime(2026, 7, 6, 22, 0, tzinfo=IST))
        self.assertEqual(end_on, datetime(2026, 7, 7, 6, 0, tzinfo=IST))


class TestLateCalculation(unittest.TestCase):
    def test_late_within_grace_period_is_zero(self):
        """09:39:59 and 09:40:00 for a 09:30 shift give 0 late minutes."""
        dt_39_59 = datetime(2026, 7, 6, 9, 39, 59, tzinfo=IST)
        dt_40_00 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        self.assertEqual(calculate_late_minutes(dt_39_59, "09:30", "18:30", "2026-07-06"), 0)
        self.assertEqual(calculate_late_minutes(dt_40_00, "09:30", "18:30", "2026-07-06"), 0)

    def test_late_one_second_past_grace_period(self):
        """09:40:01 for a 09:30 shift gives 10 late minutes."""
        dt_40_01 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        self.assertEqual(calculate_late_minutes(dt_40_01, "09:30", "18:30", "2026-07-06"), 10)

    def test_late_45_minutes_example(self):
        """10:15:59 for a 09:30 shift gives 45 late minutes."""
        dt_10_15_59 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
        self.assertEqual(calculate_late_minutes(dt_10_15_59, "09:30", "18:30", "2026-07-06"), 45)

    def test_fractional_seconds_discarded_before_late_calculation(self):
        """09:40:00.900 truncates to 09:40:00, which is inside grace period -> 0."""
        dt_with_micros = datetime(2026, 7, 6, 9, 40, 0, 900000, tzinfo=IST)
        self.assertEqual(calculate_late_minutes(dt_with_micros, "09:30", "18:30", "2026-07-06"), 0)


class TestOvertimeCalculation(unittest.TestCase):
    def test_overtime_under_30_minutes_is_zero(self):
        """10 minutes and 29m59s after shift_end give 0 overtime."""
        end_plus_10m = datetime(2026, 7, 6, 18, 40, 0, tzinfo=IST)
        end_plus_29m59s = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
        self.assertEqual(calculate_overtime_minutes(end_plus_10m, "09:30", "18:30", "2026-07-06"), 0)
        self.assertEqual(calculate_overtime_minutes(end_plus_29m59s, "09:30", "18:30", "2026-07-06"), 0)

    def test_overtime_exactly_30_minutes(self):
        """Exactly 30 minutes after shift_end gives 30 overtime minutes."""
        end_plus_30m = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
        self.assertEqual(calculate_overtime_minutes(end_plus_30m, "09:30", "18:30", "2026-07-06"), 30)

    def test_overtime_fractional_minutes_floored(self):
        """40 minutes and 59 seconds after shift_end gives 40 overtime minutes."""
        end_plus_40m59s = datetime(2026, 7, 6, 19, 10, 59, tzinfo=IST)
        self.assertEqual(calculate_overtime_minutes(end_plus_40m59s, "09:30", "18:30", "2026-07-06"), 40)

    def test_overnight_overtime_against_next_day_shift_end(self):
        """Overnight shift (22:00 - 06:00), punch-out at 06:40 next day gives 40 overtime."""
        punch_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
        self.assertEqual(calculate_overtime_minutes(punch_out, "22:00", "06:00", "2026-07-06"), 40)


class TestWorkHoursAndHalfDay(unittest.TestCase):
    def test_work_hours_sample_9_12(self):
        """09:28 to 18:35 (9h 7m) rounds to 9.12 hours (R4)."""
        p_in = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
        p_out = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
        hours = calculate_work_hours(p_in, p_out)
        self.assertEqual(hours, 9.12)
        self.assertFalse(calculate_half_day(hours))

    def test_half_day_boundary_4_49_vs_4_50(self):
        """Work hours 4.49 is half_day=True, 4.50 is half_day=False (R5)."""
        self.assertTrue(calculate_half_day(4.49))
        self.assertFalse(calculate_half_day(4.50))
        self.assertFalse(calculate_half_day(4.51))
        self.assertFalse(calculate_half_day(None))

    def test_half_day_computed_from_work_hours(self):
        """3.5 hours worked gives half_day=True."""
        p_in = datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST)
        p_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=IST)  # 3.5 hours
        hours = calculate_work_hours(p_in, p_out)
        self.assertEqual(hours, 3.5)
        self.assertTrue(calculate_half_day(hours))

    def test_invalid_interval_rejected(self):
        """punch_out <= punch_in or interval > 24 hours must raise ValueError."""
        p1 = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        p2 = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        p_earlier = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
        p_too_far = p1 + timedelta(hours=24, seconds=1)

        with self.assertRaises(ValueError):
            calculate_work_hours(p1, p2)
        with self.assertRaises(ValueError):
            calculate_work_hours(p1, p_earlier)
        with self.assertRaises(ValueError):
            calculate_work_hours(p1, p_too_far)


class TestRoundHalfUp(unittest.TestCase):
    def test_round_half_up_2_decimals(self):
        """Verify decimal ROUND_HALF_UP with 2 decimals."""
        self.assertEqual(round_half_up(2.555, 2), 2.56)
        self.assertEqual(round_half_up(2.554, 2), 2.55)
        self.assertEqual(round_half_up(2.5550, 2), 2.56)
        self.assertEqual(round_half_up(Decimal("9.116666"), 2), 9.12)
        self.assertIsNone(round_half_up(None, 2))

    def test_round_half_up_4_decimals(self):
        """Verify decimal ROUND_HALF_UP with 4 decimals (rates / percentages)."""
        self.assertEqual(round_half_up(0.12345, 4), 0.1235)
        self.assertEqual(round_half_up(0.12344, 4), 0.1234)
        self.assertEqual(round_half_up(0.95678, 4), 0.9568)


if __name__ == "__main__":
    unittest.main()
