from datetime import datetime

import pytest

from chrome_jumper.schedule import next_run, parse_hhmm


def dt(s):
    return datetime.fromisoformat(f"2026-10-09T{s}") if "T" not in s else datetime.fromisoformat(s)


def at(now, start, interval, end=None, last=None):
    return next_run(dt(now), start_mode="at", start_time=start, interval_min=interval, end_time=end,
                    last_started=dt(last) if last else None)


def test_now_mode_first_run_is_immediate_then_interval():
    now = dt("10:00:00")
    assert next_run(now, start_mode="now", start_time="", interval_min=30) == now
    assert next_run(dt("10:00:20"), start_mode="now", start_time="", interval_min=30,
                    last_started=now) == dt("10:30:00")


def test_now_mode_overrun_skips_missed_slots():
    # 10:00 시작한 실행이 10:45에 끝남 → 10:30 슬롯은 건너뛰고 11:00
    assert next_run(dt("10:45:00"), start_mode="now", start_time="", interval_min=30,
                    last_started=dt("10:00:00")) == dt("11:00:00")


def test_at_mode_before_start_waits_for_start():
    assert at("07:10:00", "09:00", 30) == dt("09:00:00")


def test_at_mode_after_start_uses_grid():
    assert at("10:31:00", "09:00", 30) == dt("11:00:00")
    assert at("11:00:00", "09:00", 30) == dt("11:00:00")


def test_at_mode_continues_past_midnight_without_end():
    assert at("23:40:00", "09:00", 60, last="23:00:00") == dt("2026-10-10T00:00:00")
    # 자정 이후에도 어제 격자를 이어서 실행
    assert at("2026-10-10T00:05:00", "09:00", 60, last="2026-10-10T00:00:00") == dt("2026-10-10T01:00:00")


def test_at_mode_with_end_time_window():
    assert at("17:59:00", "09:00", 60, end="18:00") == dt("18:00:00")
    assert at("18:01:00", "09:00", 60, end="18:00") == dt("2026-10-10T09:00:00")
    assert at("08:00:00", "09:00", 60, end="18:00") == dt("09:00:00")


def test_at_mode_end_time_crossing_midnight():
    assert at("23:30:00", "22:00", 60, end="02:00") == dt("2026-10-10T00:00:00")
    assert at("2026-10-10T01:30:00", "22:00", 60, end="02:00") == dt("2026-10-10T02:00:00")
    assert at("2026-10-10T02:30:00", "22:00", 60, end="02:00") == dt("2026-10-10T22:00:00")


def test_validation():
    with pytest.raises(ValueError):
        parse_hhmm("25:00")
    with pytest.raises(ValueError):
        parse_hhmm("9시")
    with pytest.raises(ValueError):
        at("10:00:00", "09:00", 0)


def test_now_mode_with_end_time_runs_only_inside_daily_window():
    def nr(now, last=None):
        return next_run(dt(now), start_mode="now", start_time="09:00", interval_min=60, end_time="18:00",
                        last_started=dt(last) if last else None)
    assert nr("10:15:00") == dt("10:15:00")  # 구간 안: 즉시
    assert nr("11:16:00", last="10:15:00") == dt("12:15:00")  # 지나친 11:15 슬롯은 건너뜀
    assert nr("17:20:00", last="17:15:00") == dt("2026-10-10T09:00:00")  # 18:15는 종료 뒤 → 다음 날 시작
    assert nr("20:00:00") == dt("2026-10-10T09:00:00")  # 구간 밖에서 전체 시작 → 다음 시작 시각
    assert nr("07:00:00") == dt("09:00:00")


def test_end_time_blank_repeats_until_stop():
    # 종료 시각이 없으면 하루 넘어도 계속
    assert next_run(dt("23:30:00"), start_mode="now", start_time="", interval_min=60,
                    last_started=dt("23:00:00")) == dt("2026-10-10T00:00:00")
