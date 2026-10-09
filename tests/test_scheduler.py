"""예약·동시성·중지·실패 격리 검증."""

import asyncio
import time
from datetime import datetime

import pytest

from chrome_jumper.config import Account, Settings
from chrome_jumper.runner import CycleResult
from chrome_jumper.scheduler import Scheduler

from .conftest import HEADLESS


def make_settings(url, *accs, **kw):
    s = Settings(target_url=url, **{"headless": HEADLESS, "step_timeout_sec": 6, "dialog_timeout_sec": 3, **kw})
    for name, uid, pw, extra in accs:
        a = Account(name=name, login_id=uid, **extra)
        a.set_password(pw)
        s.accounts.append(a)
    return s


def wait_until(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, data):
        self.events.append((kind, data))

    def logs(self, who=None):
        return [d["msg"] for k, d in self.events if k == "log" and (who is None or d["account"] == who)]


# ---------------------------------------------------------------- 가짜 실행 함수로 예약 로직만 검증

def test_periodic_runs_and_next_run_shown():
    calls = []

    async def fake(opts, uid, pw, log):
        calls.append((uid, datetime.now()))
        return CycleResult("성공", "ok")

    rec = Recorder()
    sch = Scheduler(rec, run_fn=fake)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="now", interval_min=0.02)))  # 1.2초
    sch.update_settings(s)
    sch.start_all()
    assert wait_until(lambda: len(calls) >= 3, 8)
    st = sch.snapshot()[s.accounts[0].id]
    assert st.status in ("대기", "실행 중", "실행 대기") and st.last_result.status == "성공"
    sch.stop_all()
    n = len(calls)
    time.sleep(1.5)
    assert len(calls) == n  # 중지 뒤 새 주기 없음
    gaps = [(b[1] - a[1]).total_seconds() for a, b in zip(calls, calls[1:])]
    assert all(g >= 1.1 for g in gaps), gaps
    sch.shutdown()


def test_changing_schedule_updates_next_run():
    async def fake(opts, uid, pw, log):
        return CycleResult("성공", "ok")

    sch = Scheduler(run_fn=fake)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="at", start_time="00:00", end_time="00:01",
                                                       interval_min=60)))
    sch.update_settings(s)
    sch.start_all()
    acc_id = s.accounts[0].id
    assert wait_until(lambda: sch.snapshot()[acc_id].next_run is not None, 3)
    first = sch.snapshot()[acc_id].next_run
    assert first > datetime.now()
    s.accounts[0].end_time = ""
    s.accounts[0].start_time = "00:00"
    s.accounts[0].interval_min = 1
    sch.update_settings(s)
    assert wait_until(lambda: sch.snapshot()[acc_id].next_run not in (None, first), 3)
    nxt = sch.snapshot()[acc_id].next_run
    assert 0 <= (nxt - datetime.now()).total_seconds() <= 61
    sch.shutdown()


def test_disabled_account_not_scheduled():
    calls = []

    async def fake(opts, uid, pw, log):
        calls.append(uid)
        return CycleResult("성공", "ok")

    sch = Scheduler(run_fn=fake)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="now", interval_min=1)),
                      ("B", "b", "p", dict(start_mode="now", interval_min=1, enabled=False)))
    sch.update_settings(s)
    sch.start_all()
    assert wait_until(lambda: "a" in calls, 3)
    time.sleep(0.5)
    assert "b" not in calls
    assert sch.snapshot()[s.accounts[1].id].status == "비활성"
    sch.shutdown()


def test_failure_isolation_and_no_overlap():
    calls = {"bad": 0, "good": 0}
    active = {"good": 0, "max": 0}

    async def fake(opts, uid, pw, log):
        calls[uid] += 1
        if uid == "bad":
            raise RuntimeError("Chrome 실행 실패(가짜)")
        active["good"] += 1
        active["max"] = max(active["max"], active["good"])
        await asyncio.sleep(2)  # 주기(0.6초)보다 긴 실행
        active["good"] -= 1
        return CycleResult("성공", "ok")

    rec = Recorder()
    sch = Scheduler(rec, run_fn=fake)
    s = make_settings("http://x/", ("BAD", "bad", "p", dict(start_mode="now", interval_min=0.01)),
                      ("GOOD", "good", "p", dict(start_mode="now", interval_min=0.01)))
    sch.update_settings(s)
    sch.start_all()
    assert wait_until(lambda: calls["good"] >= 2 and calls["bad"] >= 3, 10)
    sch.stop_all()
    assert active["max"] == 1  # 같은 계정은 겹쳐 실행되지 않음
    assert any("주기" in m and "건너뜁니다" in m for m in rec.logs("GOOD"))
    assert sch.snapshot()[s.accounts[0].id].last_result.status == "실패"
    sch.shutdown()


def test_stop_cancels_running_cycle_and_cleans_up():
    cleaned = []

    async def fake(opts, uid, pw, log):
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.append(uid)  # 실제 실행기에서는 이 위치에서 Chrome을 닫는다

    sch = Scheduler(run_fn=fake)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="now", interval_min=5)))
    sch.update_settings(s)
    sch.start_all()
    acc_id = s.accounts[0].id
    assert wait_until(lambda: sch.snapshot()[acc_id].status == "실행 중", 3)
    t0 = time.time()
    sch.stop_all()
    assert time.time() - t0 < 3
    assert cleaned == ["a"]
    st = sch.snapshot()[acc_id]
    assert st.status == "정지" and st.last_result.status == "중지됨" and st.next_run is None
    sch.shutdown()


def test_cycle_timeout_marks_failure():
    async def fake(opts, uid, pw, log):
        await asyncio.sleep(30)

    sch = Scheduler(run_fn=fake)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="now", interval_min=5)), cycle_timeout_sec=1)
    sch.update_settings(s)
    sch.run_now(s.accounts[0].id)
    assert wait_until(lambda: (r := sch.snapshot()[s.accounts[0].id].last_result) is not None, 5)
    assert "시간 제한" in sch.snapshot()[s.accounts[0].id].last_result.message
    sch.shutdown()


# ---------------------------------------------------------------- 실제 Chrome + 모의 사이트 통합

def test_real_concurrent_accounts_isolated(fresh):
    rec = Recorder()
    sch = Scheduler(rec)
    s = make_settings(fresh["url"],
                      ("계정1", "test1", "pass1", dict(start_mode="now", interval_min=30)),
                      ("계정2", "test2", "pass2", dict(start_mode="now", interval_min=30)),
                      ("틀린비번", "test3", "WRONG", dict(start_mode="now", interval_min=30)))
    sch.update_settings(s)
    sch.start_all()
    ids = [a.id for a in s.accounts]
    assert wait_until(lambda: all((st := sch.snapshot()[i]).last_result is not None for i in ids), 90)
    snap = sch.snapshot()
    sch.stop_all()
    sch.shutdown()
    assert snap[ids[0]].last_result.status == "성공"
    assert snap[ids[1]].last_result.status == "성공"
    assert snap[ids[2]].last_result.status == "실패"
    assert "로그인 실패" in snap[ids[2]].last_result.message  # 페이지 전환 중 오판 없이 실패 원인 기록
    # 쿠키·세션 분리: 각 계정의 점프는 자기 사용자로만 기록되고 세션 ID도 다르다
    log = fresh["state"].log
    assert sorted(e["user"] for e in log) == ["test1"] * 4 + ["test2"] * 4
    assert len({e["sid"] for e in log if e["user"] == "test1"}) == 1
    assert {e["sid"] for e in log if e["user"] == "test1"}.isdisjoint({e["sid"] for e in log if e["user"] == "test2"})
    # 동시에 진행: 두 계정 실행 구간이 겹친다
    a, b = snap[ids[0]].last_result, snap[ids[1]].last_result
    assert a.started < b.finished and b.started < a.finished
    # 다음 실행 시각이 30분 뒤로 잡힘(중지 전 스냅샷)
    assert snap[ids[0]].next_run is not None and (snap[ids[0]].next_run - a.started).total_seconds() >= 29 * 60
    for who in ("계정1", "계정2", "틀린비번"):
        assert "Chrome 닫음" in rec.logs(who)


def test_real_stop_closes_chrome(fresh):
    rec = Recorder()
    sch = Scheduler(rec)
    s = make_settings(fresh["url"], ("느림", "slow", "slow1", dict(start_mode="now", interval_min=30)),
                      step_timeout_sec=60)
    sch.update_settings(s)
    sch.start_all()
    assert wait_until(lambda: "로그인 성공" in rec.logs("느림") or "Chrome 실행(독립 세션)" in rec.logs("느림"), 20)
    time.sleep(2)
    sch.stop_all()
    st = sch.snapshot()[s.accounts[0].id]
    sch.shutdown()
    assert st.last_result.status == "중지됨"
    assert "Chrome 닫음" in rec.logs("느림")


# ---------------------------------------------------------------- QA 결함: GUI 전체 시작 10초 멈춤(스레드 교착)

def test_gui_calls_never_block_when_scheduler_thread_is_busy():
    """스케줄러 스레드가 잠시 막혀도(예: Tk 객체 정리로 메인 스레드를 기다림) GUI용 호출은 즉시 돌아와야 한다.

    결함 당시 start_all()이 결과를 10초 동기로 기다려, 스케줄러 스레드가 메인 스레드를 기다리는 순간 교착됐다.
    """
    async def fake(opts, uid, pw, log):
        return CycleResult("성공", "ok")

    sch = Scheduler(run_fn=fake)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="now", interval_min=1)))
    try:
        sch._loop.call_soon_threadsafe(time.sleep, 1.5)  # 스케줄러 스레드를 1.5초 붙잡음
        time.sleep(0.05)
        t0 = time.time()
        sch.update_settings(s)
        sch.start_all()
        sch.run_now(s.accounts[0].id)
        fut = sch.stop_all(wait=None)
        assert time.time() - t0 < 0.2  # 하나도 기다리지 않음
        fut.result(10)  # 스레드가 풀리면 정상 처리됨
    finally:
        sch.shutdown()


def test_start_during_stop_is_serialized_and_runs():
    """전체 중지가 진행 중일 때 전체 시작을 누르면 중지가 끝난 뒤 시작되고, 새 예약이 지워지지 않는다."""
    calls = []

    async def slow_close(opts, uid, pw, log):
        calls.append(uid)
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.5)  # Chrome 닫기 같은 정리 시간
        return CycleResult("성공", "ok")

    sch = Scheduler(run_fn=slow_close)
    s = make_settings("http://x/", ("A", "a", "p", dict(start_mode="now", interval_min=5)))
    sch.update_settings(s)
    sch.start_all()
    acc_id = s.accounts[0].id
    try:
        assert wait_until(lambda: sch.snapshot()[acc_id].status == "실행 중", 3)
        stop = sch.stop_all(wait=None)
        sch.start_all()  # 중지 정리 중에 시작
        stop.result(10)
        assert wait_until(lambda: len(calls) >= 2, 5)  # 중지 후 다시 시작되어 새 주기 실행
        assert sch.running and acc_id in sch._loops
    finally:
        sch.shutdown()
