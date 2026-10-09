"""배포 점검용 --selftest 모드(패키징 앱 스모크 테스트에 쓰임)."""

import json

from chrome_jumper import selftest


def test_selftest_runs_cycle_and_hides_password(fresh, tmp_path, monkeypatch):
    monkeypatch.delenv("CHROME_JUMPER_DATA", raising=False)
    out = tmp_path / "r.json"
    code = selftest.run(["--selftest", "--url", fresh["url"], "--account", "A:test1:pass1",
                         "--account", "B:test2:WRONG", "--out", str(out), "--timeout", "90"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1 and data["ok"] is False  # B는 일부러 실패
    by = {r["name"]: r for r in data["results"]}
    assert by["A"]["status"] == "성공" and len(by["A"]["jumps"]) == 4
    assert by["B"]["status"] == "실패" and "로그인 실패" in by["B"]["message"]
    assert data["chrome_launched"] == data["chrome_closed"] == 2
    text = out.read_text(encoding="utf-8")
    assert "pass1" not in text and "WRONG" not in text


def test_selftest_bad_args(tmp_path):
    out = tmp_path / "r.json"
    assert selftest.run(["--selftest", "--url", "ftp://x", "--account", "A:a:p", "--out", str(out)]) == 2
    assert "인자 오류" in json.loads(out.read_text(encoding="utf-8"))["error"]
