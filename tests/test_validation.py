"""반복 주기 등 입력 검증 회귀 테스트 (QA AC5: inf/1e20 주기가 저장되고 예약이 조용히 멈추던 결함)."""

import json
import math
import time
import tkinter as tk
from datetime import datetime

import pytest

from chrome_jumper.config import Account, Settings
from chrome_jumper.runner import CycleResult
from chrome_jumper.schedule import INTERVAL_MAX, check_interval, next_run
from chrome_jumper.scheduler import Scheduler

BAD = [float("inf"), float("-inf"), float("nan"), 1e20, INTERVAL_MAX + 1, 0, -5, "10", None, True]


@pytest.mark.parametrize("value", BAD)
def test_check_interval_rejects(value):
    with pytest.raises(ValueError):
        check_interval(value)


@pytest.mark.parametrize("value", [float("inf"), 1e20, float("nan")])
def test_next_run_rejects_non_runnable_interval(value):
    with pytest.raises(ValueError):
        next_run(datetime.now(), start_mode="now", start_time="", interval_min=value, last_started=datetime.now())
    with pytest.raises(ValueError):
        next_run(datetime.now(), start_mode="at", start_time="09:00", interval_min=value)


@pytest.mark.parametrize("value", BAD + [0.5])
def test_account_validate_rejects(value):
    a = Account(name="A", login_id="a", interval_min=value)
    with pytest.raises(ValueError):
        a.validate()


@pytest.mark.parametrize("value", [1, 30, 60.0, INTERVAL_MAX])
def test_account_validate_accepts(value):
    a = Account(name="A", login_id="a", interval_min=value)
    a.validate()
    assert a.interval_min == float(value)


def test_scheduler_shows_config_error_instead_of_stalling():
    """QA 재현 시나리오: 실행 중 계정 주기를 inf/1e20으로 바꾸면 '설정 오류'로 표시되고, 다른 계정은 계속 돈다."""
    calls = []

    async def fake(opts, uid, pw, log):
        calls.append(uid)
        return CycleResult("성공", "ok")

    events = []
    sch = Scheduler(lambda k, d: events.append((k, d)), run_fn=fake)
    s = Settings(target_url="http://x/")
    for name, val in (("정상", 0.02), ("무한", float("inf")), ("거대", 1e20)):
        a = Account(name=name, login_id=name, start_mode="now", interval_min=val)
        a.set_password("p")
        s.accounts.append(a)
    sch.update_settings(s)
    sch.start_all()
    try:
        ids = [a.id for a in s.accounts]
        deadline = time.time() + 5
        while time.time() < deadline and calls.count("정상") < 2:
            time.sleep(0.05)
        snap = sch.snapshot()
        assert calls.count("정상") >= 2  # 다른 계정은 영향 없음
        for i in ids[1:]:
            assert snap[i].status == "설정 오류" and snap[i].next_run is None
        assert "무한" not in calls and "거대" not in calls
        logs = [d["msg"] for k, d in events if k == "log" and d["account"] in ("무한", "거대")]
        assert sum("예약 계산 실패" in m for m in logs) == 2
        # 값을 고치면 다시 예약된다
        s.accounts[1].interval_min = 30
        sch.update_settings(s)
        deadline = time.time() + 3
        while time.time() < deadline and "무한" not in calls:
            time.sleep(0.05)
        assert "무한" in calls and sch.snapshot()[ids[1]].status in ("대기", "실행 중", "실행 대기")
    finally:
        sch.shutdown()


def test_settings_load_rejects_bad_values_with_errors(tmp_path):
    good = Account(name="정상", login_id="g", interval_min=30)
    good.set_password("p")
    raw = {
        "target_url": "http://127.0.0.1:1/owner",
        "step_timeout_sec": float("inf"), "max_concurrent": 0,
        "accounts": [
            {**good.__dict__},
            {"name": "무한", "login_id": "a", "interval_min": float("inf"), "enabled": True},
            {"name": "거대", "login_id": "b", "interval_min": 1e20, "enabled": True},
            {"name": "NaN", "login_id": "c", "interval_min": float("nan"), "enabled": True},
            {"name": "문자", "login_id": "d", "interval_min": "abc", "enabled": True},
            {"name": "시각", "login_id": "e", "interval_min": 10, "start_time": "25:99", "enabled": True},
        ],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")  # Infinity/NaN 리터럴 포함
    s = Settings.load(p)
    by = {a.name: a for a in s.accounts}
    assert by["정상"].enabled and by["정상"].interval_min == 30
    for n in ("무한", "거대", "NaN", "문자", "시각"):
        assert by[n].enabled is False, n
        assert any(f"'{n}'" in e for e in s.load_errors), (n, s.load_errors)
        by[n].validate()  # 기본값으로 정리돼 다시 활성화할 수 있는 상태
        assert math.isfinite(by[n].interval_min)
    assert s.step_timeout_sec == 20 and s.max_concurrent == 4
    assert any("step_timeout_sec" in e for e in s.load_errors)
    s.save(p)  # inf/nan 없이 정상 저장
    again = Settings.load(p)
    assert len([e for e in again.load_errors if "계정" in e]) == 0
    assert "load_errors" not in json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tk_root():
    # pytest의 출력 캡처 중에는 Tk 생성이 가끔 'usable tk.tcl'을 못 찾는다(실제 앱 실행과 무관, 재시도로 해결).
    last = None
    for _ in range(5):
        try:
            root = tk.Tk()
            break
        except tk.TclError as exc:
            last = exc
            time.sleep(0.2)
    else:
        raise last
    root.withdraw()
    yield root
    root.destroy()


@pytest.mark.parametrize("text", ["inf", "1e20", "nan", "-inf", "0", "0.5", "abc", "99999"])
def test_gui_dialog_rejects_bad_interval(text, monkeypatch, tk_root):
    from tkinter import messagebox

    from chrome_jumper.app import AccountDialog
    errors = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg, **k: errors.append(msg))
    d = AccountDialog(tk_root, None)
    try:
        d.v_name.set("A"); d.v_id.set("a"); d.v_pw.set("p"); d.v_interval.set(text)
        d._ok()
        assert d.result is None and errors and "반복 주기" in errors[-1]
        d.v_interval.set("15")
        d._ok()
        assert d.result is not None and d.result[0].interval_min == 15
    finally:
        if d.winfo_exists():
            d.destroy()


@pytest.mark.parametrize("field,text", [("v_step", "inf"), ("v_dialog", "nan"), ("v_cycle", "1e20"),
                                        ("v_verify", "0"), ("v_step", "abc")])
def test_gui_advanced_rejects_bad_timeouts(field, text, monkeypatch, tk_root):
    from tkinter import messagebox

    from chrome_jumper.app import AdvancedDialog
    errors = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg, **k: errors.append(msg))
    s = Settings()
    d = AdvancedDialog(tk_root, s)
    try:
        getattr(d, field).set(text)
        d._ok()
        assert not d.ok and errors
        assert s.step_timeout_sec == 20 and s.cycle_timeout_sec == 180  # 변경되지 않음
    finally:
        if d.winfo_exists():
            d.destroy()
