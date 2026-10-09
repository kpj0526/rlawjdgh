"""모의 사이트 + 실제 Chrome으로 한 주기 흐름 검증."""

import asyncio

import pytest

from chrome_jumper.runner import COOLDOWN, FAILED, NO_BUTTON, NO_DIALOG, OK, RunOptions, run_cycle


def run(opts, uid, pw):
    logs = []
    res = asyncio.run(run_cycle(opts, uid, pw, lambda lvl, msg: logs.append((lvl, msg))))
    return res, logs


def outcomes(res):
    return [j.outcome for j in res.jumps]


def assert_closed(logs):
    msgs = [m for _, m in logs]
    assert msgs.count("Chrome 실행(독립 세션)") == msgs.count("Chrome 닫음") == 1


def test_success_clicks_four_and_confirms(opts, fresh):
    res, logs = run(opts, "test1", "pass1")
    assert res.status == "성공", res.message
    assert outcomes(res) == [OK] * 4
    assert all("점프할까요?" in j.detail for j in res.jumps)  # 확인 창 메시지를 승인함
    assert fresh["state"].count("test1", "lineup") == 1
    assert sorted(e["type"] for e in fresh["state"].log) == ["lineup", "manager", "promo", "realtime"]
    assert_closed(logs)
    assert not any("pass1" in m for _, m in logs)  # 비밀번호는 로그에 남지 않음


def test_second_run_during_cooldown_is_skipped(opts, fresh):
    run(opts, "test1", "pass1")
    res, _ = run(opts, "test1", "pass1")
    assert res.status == "건너뜀"
    assert outcomes(res) == [COOLDOWN] * 4
    assert len(fresh["state"].log) == 4  # 추가 점프 없음


def test_cycle_again_after_cooldown(opts, fresh):
    fresh["state"].cooldown = 0
    assert run(opts, "test1", "pass1")[0].status == "성공"
    assert run(opts, "test1", "pass1")[0].status == "성공"
    assert len(fresh["state"].log) == 8


def test_wrong_password(opts, fresh):
    res, logs = run(opts, "test2", "wrong")
    assert res.status == "실패"
    assert "로그인 실패" in res.message and "일치하지" in res.message
    assert fresh["state"].log == []
    assert_closed(logs)


@pytest.mark.parametrize("user,expected,status", [
    ("cool", [COOLDOWN] * 4, "건너뜀"),
    ("nobtn", [OK, OK, OK, NO_BUTTON], "부분 성공"),
    ("nodialog", [OK, OK, NO_DIALOG, OK], "부분 성공"),
    ("limit", [FAILED] * 4, "실패"),
    ("modal", [OK] * 4, "성공"),
])
def test_site_variants(opts, fresh, user, expected, status):
    res, logs = run(opts, user, user + "1")
    assert outcomes(res) == expected, res.jumps
    assert res.status == status
    assert_closed(logs)


def test_captcha_is_reported_not_bypassed(opts, fresh):
    res, logs = run(opts, "captcha", "captcha1")
    assert res.status == "실패" and "CAPTCHA" in res.message
    assert_closed(logs)


def test_network_delay_times_out(opts, fresh):
    res, logs = run(opts, "slow", "slow1")  # 모의 지연 30초 > 단계 제한 6초
    assert res.status == "실패"
    assert fresh["state"].log == []
    assert_closed(logs)


def test_login_page_as_target_url(fresh):
    o = RunOptions(url=fresh["url"].replace("/owner", "/login"), jump_labels=__import__(
        "chrome_jumper.config", fromlist=["DEFAULT_JUMPS"]).DEFAULT_JUMPS, headless=True,
        step_timeout_sec=6, dialog_timeout_sec=3)
    res, _ = run(o, "test3", "pass3")
    assert res.status == "성공", res.message


def test_unreachable_url(opts):
    o = RunOptions(**{**opts.__dict__, "url": "http://127.0.0.1:1/owner"})
    res, logs = run(o, "test1", "pass1")
    assert res.status == "실패" and "페이지 열기 실패" in res.message
    assert_closed(logs)


def test_chrome_launch_failure(opts):
    o = RunOptions(**{**opts.__dict__, "chrome_path": r"C:\nope\chrome.exe"})
    res, _ = run(o, "test1", "pass1")
    assert res.status == "실패" and "Chrome 실행 실패" in res.message
