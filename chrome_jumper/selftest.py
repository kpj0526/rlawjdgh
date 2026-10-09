"""배포 파일 점검용 자체 시험 모드(사용자 기능 아님).

ChromeJumper.exe --selftest --url http://127.0.0.1:8765/owner --account 이름:아이디:비밀번호 [...] --out 결과.json
    [--click-gap 초] [--confirm-delay 초]   (점프 간격 확인용. 결과 JSON에 jump_sec·간격 값이 남는다)

- 임시 데이터 폴더를 쓰고 사용자 설정(%APPDATA%\\ChromeJumper)은 읽거나 쓰지 않는다.
- 지정 계정들을 '즉시 시작'으로 스케줄러에 올려 전체 시작 → 모든 계정 1주기 완료 → 전체 중지까지 실행하고
  결과를 JSON으로 저장한다. 비밀번호는 결과·로그에 쓰지 않는다.
- 종료 코드: 모든 계정 성공 0, 그 밖 1, 인자 오류 2.
- 패키징된 실행 파일 안에서 Playwright 드라이버·시스템 Chrome·스케줄러가 동작하는지 확인하는 용도다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import PACE_RANGE, Account, Settings, validate_url


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ChromeJumper --selftest", add_help=False)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--url", required=True)
    ap.add_argument("--account", action="append", required=True, help="이름:아이디:비밀번호")
    ap.add_argument("--out", required=True)
    ap.add_argument("--headed", action="store_true", help="Chrome 창을 띄움(기본은 숨김)")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--click-gap", type=float, default=None, help="클릭 간격(초). 생략하면 기본값")
    ap.add_argument("--confirm-delay", type=float, default=None, help="확인 승인 지연(초). 생략하면 기본값")
    return ap.parse_args(argv)


def run(argv: list[str]) -> int:
    try:
        a = _parse(argv)
        url = validate_url(a.url)
        for name, v in (("--click-gap", a.click_gap), ("--confirm-delay", a.confirm_delay)):
            if v is not None and not (PACE_RANGE[0] <= v <= PACE_RANGE[1]):
                raise ValueError(f"{name}는 {PACE_RANGE[0]}~{PACE_RANGE[1]}초여야 합니다: {v}")
        accounts = []
        for spec in a.account:
            name, uid, pw = spec.split(":", 2)
            acc = Account(name=name, login_id=uid, start_mode="now", interval_min=60)
            acc.set_password(pw)
            acc.validate()
            accounts.append(acc)
    except (SystemExit, ValueError) as exc:
        _write(Path(argv[argv.index("--out") + 1]) if "--out" in argv else None,
               {"ok": False, "error": f"인자 오류: {exc}"})
        return 2

    from .scheduler import Scheduler

    # 사용자 데이터 폴더를 건드리지 않도록 이 실행 동안만 임시 폴더를 쓴다.
    old_data = os.environ.get("CHROME_JUMPER_DATA")
    os.environ["CHROME_JUMPER_DATA"] = tempfile.mkdtemp(prefix="cj-selftest-")

    logs: list[str] = []

    def on_event(kind, data):
        if kind == "log":
            logs.append(f"{data['time']:%H:%M:%S} [{data['account']}] {data['msg']}")

    s = Settings(target_url=url, accounts=accounts, headless=not a.headed)
    if a.click_gap is not None:
        s.click_gap_sec = a.click_gap
    if a.confirm_delay is not None:
        s.confirm_delay_sec = a.confirm_delay
    sch = Scheduler(on_event)
    started = time.time()
    try:
        sch.update_settings(s, wait=10)
        sch.start_all(wait=10)
        ids = [acc.id for acc in accounts]
        while time.time() - started < a.timeout:
            snap = sch.snapshot()
            if all(snap[i].last_result is not None for i in ids):
                break
            time.sleep(0.5)
        snap = sch.snapshot()
    finally:
        sch.shutdown()
        if old_data is None:
            os.environ.pop("CHROME_JUMPER_DATA", None)
        else:
            os.environ["CHROME_JUMPER_DATA"] = old_data

    results = []
    for acc in accounts:
        st = snap.get(acc.id)
        r = st.last_result if st else None
        results.append({
            "name": acc.name, "login_id": acc.login_id,
            "status": r.status if r else "결과 없음(시간 초과)",
            "message": r.message if r else "",
            "jump_sec": round(r.jump_sec, 2) if r and r.jump_sec is not None else None,
            "jumps": [{"label": j.label, "outcome": j.outcome, "detail": j.detail} for j in (r.jumps if r else [])],
        })
    ok = all(x["status"] == "성공" for x in results)
    _write(Path(a.out), {
        "ok": ok, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable, "time": datetime.now().isoformat(timespec="seconds"),
        "elapsed_sec": round(time.time() - started, 1),
        "click_gap_sec": s.click_gap_sec, "confirm_delay_sec": s.confirm_delay_sec, "results": results,
        "chrome_closed": sum("Chrome 닫음" in x for x in logs),
        "chrome_launched": sum("Chrome 실행(독립 세션)" in x for x in logs),
        "log": logs,
    })
    return 0 if ok else 1


def _write(path: Path | None, data: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
