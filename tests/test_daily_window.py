"""요청 005: 앱을 켜 둔 채 '전체 시작' 한 번으로 매일 09:00~다음 날 01:00 반복, 01:00 이후 대기,
다음 날 09:00 자동 재개, '전체 중지' 시 정지. 가짜 시계로 하루 이상을 빠르게 흘려 실제 Scheduler를 검증한다."""

import time
from datetime import datetime, timedelta

import pytest

from chrome_jumper.config import Account, Settings
from chrome_jumper.runner import CycleResult
from chrome_jumper.scheduler import Scheduler


def D(s):
    return datetime.fromisoformat(s)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def wait_until(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def env():
    clock = Clock(D("2026-10-09T08:59:00"))
    calls = []

    async def fake(opts, uid, pw, log):
        calls.append(clock())
        return CycleResult("성공", "ok")

    events = []
    sch = Scheduler(lambda k, d: events.append((k, d)), run_fn=fake, now_fn=clock, max_wait_sec=0.02)
    yield sch, clock, calls, events
    sch.shutdown()


def setup(sch, **acc):
    s = Settings(target_url="http://x/")
    a = Account(name="A", login_id="a", **acc)
    a.set_password("p")
    s.accounts.append(a)
    sch.update_settings(s)
    return a.id


def idle_at(sch, acc_id, nxt):
    """실행이 끝나고 '대기' 상태로 다음 실행 시각이 nxt로 잡힐 때까지."""
    return wait_until(lambda: (st := sch.snapshot()[acc_id]).status == "대기" and st.next_run == nxt)


def test_at_mode_runs_window_waits_overnight_and_resumes_next_day(env):
    sch, clock, calls, events = env
    acc = setup(sch, start_mode="at", start_time="09:00", end_time="01:00", interval_min=60)
    sch.start_all()
    assert idle_at(sch, acc, D("2026-10-09T09:00:00"))
    time.sleep(0.2)
    assert calls == []  # 시작 시각 전에는 실행하지 않음

    slots = [D("2026-10-09T09:00:00") + timedelta(hours=h) for h in range(17)]  # 09:00 ... 다음 날 01:00
    for i, slot in enumerate(slots):
        clock.t = slot
        assert wait_until(lambda: len(calls) == i + 1), (slot, calls)
        nxt = slots[i + 1] if i + 1 < len(slots) else D("2026-10-10T09:00:00")
        assert idle_at(sch, acc, nxt), (slot, sch.snapshot()[acc])
    assert calls == slots  # 01:00 정각까지 매시 실행

    # 01:00 이후: 쉬는 동안 실행하지 않고 '대기'(전체 중지 아님), 다음 실행은 다음 날 09:00
    for t in ("2026-10-10T01:00:30", "2026-10-10T01:30:00", "2026-10-10T05:00:00", "2026-10-10T08:59:59"):
        clock.t = D(t)
        time.sleep(0.15)
        st = sch.snapshot()[acc]
        assert len(calls) == 17 and st.status == "대기" and st.next_run == D("2026-10-10T09:00:00"), (t, st)
        assert sch.running

    # 다음 날 09:00 자동 재개, 이후 주기 반복
    clock.t = D("2026-10-10T09:00:00")
    assert wait_until(lambda: len(calls) == 18) and calls[-1] == D("2026-10-10T09:00:00")
    assert idle_at(sch, acc, D("2026-10-10T10:00:00"))
    clock.t = D("2026-10-10T10:00:00")
    assert wait_until(lambda: len(calls) == 19)
    assert idle_at(sch, acc, D("2026-10-10T11:00:00"))

    # 전체 중지 뒤에는 시간이 흘러도 실행하지 않음
    sch.stop_all()
    assert not sch.running and sch.snapshot()[acc].status == "정지" and sch.snapshot()[acc].next_run is None
    for t in ("2026-10-10T11:00:00", "2026-10-11T09:00:00"):
        clock.t = D(t)
        time.sleep(0.15)
    assert len(calls) == 19
    assert any(d["msg"] == "전체 중지 완료" for k, d in events if k == "log")


def test_now_mode_window_resumes_next_day(env):
    """시작 방식 '즉시' + 종료 시각: 전체 시작 즉시 실행, 01:00 넘으면 쉬고 다음 날 09:00 재개."""
    sch, clock, calls, _ = env
    clock.t = D("2026-10-09T23:10:00")
    acc = setup(sch, start_mode="now", start_time="09:00", end_time="01:00", interval_min=30)
    sch.start_all()
    assert wait_until(lambda: len(calls) == 1) and calls[0] == D("2026-10-09T23:10:00")
    for t in ("2026-10-09T23:40:00", "2026-10-10T00:10:00", "2026-10-10T00:40:00"):
        assert idle_at(sch, acc, D(t))
        clock.t = D(t)
        assert wait_until(lambda: calls[-1] == D(t))
    # 다음 슬롯 01:10은 종료(01:00) 뒤 → 다음 날 09:00
    assert idle_at(sch, acc, D("2026-10-10T09:00:00"))
    clock.t = D("2026-10-10T03:00:00")
    time.sleep(0.15)
    assert len(calls) == 4
    clock.t = D("2026-10-10T09:00:00")
    assert wait_until(lambda: len(calls) == 5)
    assert idle_at(sch, acc, D("2026-10-10T09:30:00"))


def test_clock_jump_during_long_wait_runs_on_wake(env):
    """PC 절전 등으로 예정 시각(09:00)을 지나 깨어나면 긴 대기를 잘게 나눠 보므로 바로 실행한다."""
    sch, clock, calls, _ = env
    clock.t = D("2026-10-10T01:30:00")
    acc = setup(sch, start_mode="at", start_time="09:00", end_time="01:00", interval_min=60)
    sch.start_all()
    assert idle_at(sch, acc, D("2026-10-10T09:00:00"))
    clock.t = D("2026-10-10T09:07:00")  # 절전에서 늦게 깸
    assert wait_until(lambda: len(calls) == 1) and calls[0] == D("2026-10-10T09:07:00")
    assert idle_at(sch, acc, D("2026-10-10T10:00:00"))


def test_settings_change_during_overnight_wait_reschedules(env):
    sch, clock, calls, _ = env
    clock.t = D("2026-10-10T02:00:00")
    acc = setup(sch, start_mode="at", start_time="09:00", end_time="01:00", interval_min=60)
    sch.start_all()
    assert idle_at(sch, acc, D("2026-10-10T09:00:00"))
    s = sch.settings
    s.accounts[0].start_time = "08:00"
    sch.update_settings(s)
    assert idle_at(sch, acc, D("2026-10-10T08:00:00"))
    assert calls == []
