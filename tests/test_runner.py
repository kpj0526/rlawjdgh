"""모의 사이트 + 실제 Chrome으로 한 주기 흐름 검증."""

import asyncio

import pytest

from chrome_jumper.runner import (COOLDOWN, FAILED, NO_BUTTON, NO_DIALOG, OK, UNVERIFIED, JumpResult, RunOptions,
                                  _overall, run_cycle)


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
    # 승인만이 아니라 사이트의 실제 성공 신호로 판정
    assert all(any(sig in j.detail for sig in ("쿨다운 전환", "횟수 표시 변화", "성공 응답")) for j in res.jumps)
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
    # 버튼별 결과·원인이 로그에 한 줄씩 남는다(요청 003: 화면 패널 대신 로그로 확인)
    for j in res.jumps:
        assert f"{j.label}: {j.outcome} - {j.detail}" in [m for _, m in logs]


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


# ---------------------------------------------------------------- QA 결함: 확인 승인만으로 성공 처리하던 문제

def test_server_rejects_jump_with_500_is_failure_not_success(opts, fresh):
    """확인 창은 승인했지만 서버가 HTTP 500으로 거부하고 횟수도 그대로 → 성공으로 기록하면 안 된다."""
    res, logs = run(opts, "failjump", "failjump1")
    assert outcomes(res) == [FAILED] * 4, res.jumps
    assert all("HTTP 500" in j.detail and "점프할까요?" in j.detail for j in res.jumps)
    assert res.status == "실패"
    assert fresh["state"].log == []  # 서버 기준 실제 점프 0회
    assert_closed(logs)


def test_no_success_signal_is_unverified_not_success(opts, fresh):
    """서버가 200(빈 응답)만 주고 화면·횟수 변화가 없으면 '확인 불가'. 성공으로 세지 않는다."""
    res, logs = run(opts, "nosignal", "nosignal1")
    assert outcomes(res) == [UNVERIFIED] * 4, res.jumps
    assert res.status == "실패"
    assert fresh["state"].log == []
    assert_closed(logs)


def test_success_detected_from_page_change_without_success_flag(opts, fresh):
    """성공 플래그가 없는 사이트: 버튼 대기 전환·횟수 변화로 성공을 확인한다."""
    res, _ = run(opts, "quiet", "quiet1")
    assert outcomes(res) == [OK] * 4, res.jumps
    assert all(("쿨다운 전환" in j.detail) or ("횟수 표시 변화" in j.detail) for j in res.jumps)
    assert len(fresh["state"].log) == 4


def test_success_detected_after_page_reload(opts, fresh):
    """점프 뒤 페이지를 새로고침하는 사이트: 새 화면에서 해당 카드가 대기 상태인지로 확인한다."""
    res, _ = run(opts, "reload", "reload1")
    assert outcomes(res) == [OK] * 4, res.jumps
    assert len(fresh["state"].log) == 4


def test_overall_never_counts_unverified_as_success():
    j = lambda o: JumpResult("x", o)  # noqa: E731
    assert _overall([j(UNVERIFIED)] * 4) == "실패"
    assert _overall([j(OK)] * 3 + [j(UNVERIFIED)]) == "부분 성공"
    assert _overall([j(OK)] * 4) == "성공"
