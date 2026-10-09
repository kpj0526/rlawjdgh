"""실행 예정 시각 계산.

규칙(README의 "예약 규칙"과 같음):
- 시작 방식 "now": 전체 시작(또는 설정 변경) 시점에 바로 1회 실행하고, 그 뒤로는
  직전 실행 *시작* 시각 + 주기마다 실행한다. 종료 시각이 없으면 시작 시각 값은 쓰지 않는다.
  종료 시각이 있으면 매일 시작~종료 구간 안에서만 실행한다(구간 밖이면 다음 시작 시각에 재개).
- 겹침 정책: 한 계정은 한 번에 한 주기만 실행한다. 주기가 길어져 다음 슬롯을 지나치면
  그 슬롯은 건너뛰고(로그에 기록) 실행이 끝난 뒤 다음 슬롯을 기다린다.
- 시작 방식 "at": 매일 시작 시각(HH:MM)을 기준점으로 주기 간격의 격자(09:00, 09:30 ...)에서
  실행한다. 오늘 시작 시각 전에 전체 시작을 누르면 시작 시각에 첫 실행을 하고,
  이미 지났으면 오늘 격자에서 지금 이후의 가장 가까운 슬롯에 첫 실행을 한다.
- 종료 시각(선택, 사용자 확정): 비우면 전체 중지까지 계속 반복한다(자정도 넘어 같은 격자).
  입력하면 매일 시작 시각~종료 시각 사이에만 실행하고 다음 날 시작 시각에 재개한다.
  종료 시각 정각 슬롯은 실행한다. 종료 시각이 시작 시각보다 이르면 자정을 넘기는 구간으로 본다.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta

# 반복 주기 허용 범위(분). 화면·설정 파일 입력은 USER_MIN 이상만 받는다.
# 예약 계산은 유한한 양수이고 MAX 이하인지만 본다(테스트용 짧은 주기 허용).
INTERVAL_USER_MIN = 1
INTERVAL_MAX = 7 * 24 * 60  # 7일


def check_interval(value, minimum: float = 0) -> float:
    """반복 주기(분)를 검사해 float로 돌려준다. 숫자가 아니거나 inf/nan/범위 밖이면 ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"반복 주기(분)는 숫자여야 합니다: {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"반복 주기(분)가 유한한 숫자가 아닙니다: {value!r}")
    if v <= 0 or v < minimum:
        raise ValueError(f"반복 주기(분)는 {minimum:g} 이상이어야 합니다: {value!r}" if minimum
                         else f"반복 주기(분)는 0보다 커야 합니다: {value!r}")
    if v > INTERVAL_MAX:
        raise ValueError(f"반복 주기(분)는 {INTERVAL_MAX}분(7일) 이하여야 합니다: {value!r}")
    return v


def parse_hhmm(value: str) -> time:
    if not isinstance(value, str):
        raise ValueError(f"시각은 HH:MM 형식이어야 합니다: {value!r}")
    value = value.strip()
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
    except ValueError as exc:
        raise ValueError(f"시각은 HH:MM 형식이어야 합니다: {value!r}") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"시각 범위가 잘못되었습니다: {value!r}")
    return time(h, m)


def _ceil_slot(anchor: datetime, now: datetime, step: timedelta) -> datetime:
    """anchor + k*step >= now 를 만족하는 가장 작은 k의 시각 (k >= 0)."""
    if now <= anchor:
        return anchor
    k = -(-(now - anchor) // step)  # ceil
    return anchor + k * step


def next_run(
    now: datetime,
    *,
    start_mode: str,
    start_time: str,
    interval_min: float,
    end_time: str | None = None,
    last_started: datetime | None = None,
) -> datetime:
    """now 이후(now 포함)의 다음 실행 시각."""
    step = timedelta(minutes=check_interval(interval_min))

    if start_mode not in ("now", "at"):
        raise ValueError(f"알 수 없는 시작 방식: {start_mode!r}")
    end_t = parse_hhmm(end_time) if end_time else None
    start_t = parse_hhmm(start_time) if (start_mode == "at" or end_t) else None

    if start_mode == "now":
        # 주기가 길어져 지나친 슬롯은 건너뛴다(겹침 정책).
        cand = now if last_started is None else _ceil_slot(last_started + step, now, step)
        if end_t is None:
            return cand
        # 종료 시각이 있으면 매일 시작~종료 구간 안에서만 실행하고, 벗어나면 다음 구간 시작에 재개.
        for win_start, win_end in _windows(cand, start_t, end_t):
            if win_end < cand:
                continue
            return max(cand, win_start)
        raise AssertionError("unreachable")  # pragma: no cover

    if end_t is None:
        # 오늘 시작 시각이 아직이면 그 시각, 이미 지났으면 오늘 격자상 다음 슬롯.
        anchor = datetime.combine(now.date(), start_t)
        if now < anchor:
            if last_started is None:
                # 첫 실행은 시작 시각보다 앞당기지 않는다.
                return anchor
            # 이미 돌고 있으면 어제 기준점 격자가 자정을 넘어 이어진다.
            prev = _ceil_slot(anchor - timedelta(days=1), now, step)
            return min(prev, anchor)
        return _ceil_slot(anchor, now, step)

    # 종료 시각이 있는 경우: 어제/오늘/내일 구간을 차례로 검사.
    for win_start, win_end in _windows(now, start_t, end_t):
        if win_end < now:
            continue
        cand = _ceil_slot(win_start, now, step)
        if cand <= win_end:
            return cand
    raise AssertionError("unreachable")  # pragma: no cover


def _windows(ref: datetime, start_t: time, end_t: time):
    """ref 전날부터 사흘 뒤까지의 매일 [시작, 종료] 구간. 종료<=시작이면 자정을 넘긴다."""
    for day_offset in (-1, 0, 1, 2):
        day = ref.date() + timedelta(days=day_offset)
        win_start = datetime.combine(day, start_t)
        win_end = datetime.combine(day, end_t)
        if win_end <= win_start:
            win_end += timedelta(days=1)
        yield win_start, win_end
