"""메인 화면 구성(요청 003: 선택 계정 버튼별 결과 패널 제거, 결과는 실행 로그로 확인).

창은 숨긴 상태(withdraw)로 만들고 화면에 띄우거나 입력을 보내지 않는다.
"""

import time
import tkinter as tk
from datetime import datetime

import pytest

from chrome_jumper import app as appmod
from chrome_jumper.runner import CycleResult, JumpResult


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROME_JUMPER_DATA", str(tmp_path))
    last = None
    for _ in range(5):  # pytest 출력 캡처 중 Tk 생성이 가끔 실패해 재시도(test_validation 참고)
        try:
            root = tk.Tk()
            break
        except tk.TclError as exc:
            last = exc
            time.sleep(0.2)
    else:
        raise last
    root.withdraw()
    a = appmod.App(root, tmp_path)
    yield a
    a.scheduler.shutdown()
    root.destroy()


def _texts(widget):
    out = []
    for w in widget.winfo_children():
        for opt in ("text",):
            try:
                out.append(str(w.cget(opt)))
            except tk.TclError:
                pass
        if isinstance(w, tk.Text):
            out.append(w.get("1.0", "end"))
        out.extend(_texts(w))
    return out


def test_button_result_panel_removed(app):
    texts = " ".join(_texts(app.root))
    assert "버튼별 결과" not in texts and "계정을 선택하세요" not in texts
    assert not hasattr(app, "tv_det") and not hasattr(app, "l_det") and not hasattr(app, "_show_detail")
    # 남은 표는 계정 표 하나(실행 로그는 Text)
    def all_widgets(w):
        yield w
        for c in w.winfo_children():
            yield from all_widgets(c)
    from tkinter import ttk
    assert [w for w in all_widgets(app.root) if isinstance(w, ttk.Treeview)] == [app.tv]
    # 핵심 조작 버튼은 그대로
    for label in ("▶ 전체 시작", "■ 전체 중지", "계정 추가", "수정", "삭제", "지금 실행", "URL 적용", "고급"):
        assert label in texts, label


def test_selecting_account_still_works_without_panel(app):
    from chrome_jumper.config import Account
    a = Account(name="가게A", login_id="a")
    a.set_password("p")
    app.settings.accounts.append(a)
    app._refresh_table()
    app.tv.selection_set(a.id)
    app.root.update()
    assert app._selected() is a


def test_log_shows_per_button_results_and_errors(app):
    """버튼별 결과·오류 원인은 실행 로그(화면)에서 확인한다."""
    t = datetime.now()
    for msg, lvl in (("라인업/PR 점프: 성공 - 확인 승인: … → 쿨다운 전환 확인(대기 10:00)", "INFO"),
                     ("홍보관 점프: 버튼 없음 - 버튼을 찾지 못했습니다.", "WARN"),
                     ("실행 종료: 부분 성공 - 라인업/PR:성공 / 홍보관:버튼 없음", "WARN")):
        app.events.put(("log", {"time": t, "account": "가게A", "level": lvl, "msg": msg}))
    app._poll()
    text = app.t_log.get("1.0", "end")
    assert "[가게A] 라인업/PR 점프: 성공" in text
    assert "[가게A] 홍보관 점프: 버튼 없음 - 버튼을 찾지 못했습니다." in text
    assert "실행 종료: 부분 성공" in text
