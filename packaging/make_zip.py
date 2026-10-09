"""PyInstaller 결과물을 배포 ZIP으로 묶고 크기·SHA-256을 기록한다. build.bat에서 호출.

산출물(dist/):
  ChromeJumper-<버전>-win64.zip           사용자 배포본
  ChromeJumper-MockSite-<버전>-win64.zip  QA·점검용 모의 사이트(사용자에게는 필요 없음)
  SHA256SUMS.txt, BUILD_INFO.json
금지 항목(설정·비밀번호·로그·가상환경·빌드 캐시·테스트)이 들어가면 실패한다.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chrome_jumper import __version__  # noqa: E402

PYI = ROOT / "build" / "pyi-dist"
DIST = ROOT / "dist"

FORBIDDEN_NAMES = ["config.json", "*.log", "crash.log", "*.tmp", "*.spec", "*.pyc.tmp", "SHA256SUMS.txt"]
FORBIDDEN_DIRS = [".venv", "venv", "__pycache__", ".pytest_cache", "logs", "pyi-work", "tests", ".git",
                  "ChromeJumper-data", "cj-selftest-*"]
FORBIDDEN_BYTES = [b"dpapi:", b"password_enc"]  # 저장된 계정 설정의 흔적

USER_README = """\
Chrome 점프 자동화 {version} (Windows)
================================

[필요 조건]
- Windows 10 또는 11 (64비트)
- Google Chrome 설치 (평소 쓰는 Chrome을 그대로 사용합니다)
- Python 등 다른 프로그램 설치는 필요 없습니다.

[실행 방법]
1. 받은 ZIP 파일을 마우스 오른쪽 버튼 → "압축 풀기"로 원하는 폴더(예: 바탕화면)에 풉니다.
   ※ ZIP 안에서 바로 실행하지 말고 반드시 압축을 푼 뒤 실행하세요.
2. 풀린 ChromeJumper 폴더 안의 ChromeJumper.exe 를 더블클릭합니다.
   - "Windows의 PC 보호" 창이 뜨면 "추가 정보" → "실행"을 누릅니다(서명되지 않은 프로그램 안내).
3. 대상 URL을 입력하고 "URL 적용", "계정 추가"로 계정을 등록한 뒤 "▶ 전체 시작"을 누릅니다.
   - 프로그램을 켜기만 해서는 아무 사이트에도 접속하지 않습니다.
   - 처음에는 "Chrome 창 숨기기"를 끄고 계정 하나를 선택해 "지금 실행"으로 동작을 확인하세요.

[저장 위치]
- 설정과 로그는 프로그램 폴더가 아니라 %APPDATA%\\ChromeJumper 에 저장됩니다.
  (주소창에 %APPDATA%\\ChromeJumper 를 입력하면 열립니다.)
- 비밀번호는 Windows 사용자 계정 기준으로 암호화되어 저장되며, 다른 PC로 옮기면 다시 입력해야 합니다.
- 새 버전으로 바꿀 때는 새 ZIP을 풀어 실행하면 기존 설정을 그대로 씁니다. 이전 폴더는 지워도 됩니다.

[예약 규칙 요약]
- 즉시 시작: 전체 시작 즉시 1회, 이후 주기마다.
- 지정 시각에 시작: 매일 시작 시각 기준 주기 간격(예: 09:00, 09:30 ...).
- 종료 시각(선택): 비우면 전체 중지까지 반복, 입력하면 매일 그 시각까지만 실행하고 다음 날 시작 시각에 재개.
  예) 09:00 시작, 01:00 종료: 프로그램을 켜 둔 채 전체 시작을 한 번 누르면 매일 09:00~다음 날 01:00에
  주기마다 실행하고, 01:00 이후에는 '대기'로 쉬다가 다음 날 09:00에 자동으로 다시 시작합니다.
  이 대기는 전체 중지가 아닙니다. 전체 중지를 누르거나 프로그램을 닫으면 멈춥니다.
- 확인 창을 승인한 뒤 사이트의 실제 성공 신호(버튼 대기 전환·횟수 변화 등)가 있어야 '성공'으로 기록합니다.

[점프 간격]
- 너무 빠른 연속 조작을 피하려고 고급 설정의 '클릭 간격'(기본 0.5초: 점프 화면이 뜬 뒤 첫 버튼 전,
  이전 버튼 판정 뒤 다음 버튼 전)과 '확인 승인 지연'(기본 0.4초: 확인 창이 뜬 뒤 승인 전)을 둡니다.
- 기본값이 실제 사이트에 맞는다는 보장은 없습니다. '너무 빠릅니다'/'잠시 후에 다시' 실패, 확인 불가,
  확인 창 없음이 반복되면 두 값을 0.5초씩 늘리고 '지금 실행'으로 확인하세요(0~10초).
- 로그인 뒤 점프 4종 처리 시간은 로그에 남습니다. 5초는 빠른 사이트 기준 목표이며, 넘어도 중단하지 않습니다.

[문제가 생기면]
- 시작되지 않으면 %APPDATA%\\ChromeJumper\\logs\\crash.log 를 확인하세요.
- "Chrome 실행 실패"가 나오면 Chrome이 설치되어 있는지 확인하거나, 고급 설정에서 chrome.exe 경로를 지정하세요.
"""

MOCK_README = """\
모의 사이트 {version} (QA·점검용, 실제 사이트 아님)
================================
MockSite.exe 를 실행하면 http://127.0.0.1:8765/owner 에서 모의 사이트가 열립니다(창을 닫으면 종료).
옵션 예: MockSite.exe --port 8770 --cooldown 0 --slow 40 --slowjump 1.5
테스트 계정(아이디/비밀번호): test1/pass1, test2/pass2, test3/pass3(정상), cool/cool1, nobtn/nobtn1,
nodialog/nodialog1, limit/limit1, modal/modal1, slow/slow1, captcha/captcha1, failjump/failjump1,
nosignal/nosignal1, quiet/quiet1, reload/reload1, pace/pace1(빠른 조작 거부), slowjump/slowjump1(느린 점프 응답)
기록 조회: http://127.0.0.1:8765/api/stats

패키징 앱 자체 시험(창 없이 1주기 실행 후 결과 JSON 저장, 종료 코드 0=모두 성공):
  ChromeJumper.exe --selftest --url http://127.0.0.1:8765/owner --account A:test1:pass1 --account B:test2:pass2 --out result.json
  간격 확인: ... --account P:pace:pace1 --out pace.json [--click-gap 0.5 --confirm-delay 0.4]  (결과의 jump_sec, /api/stats의 requests 시각)
"""


def _text(s: str) -> bytes:
    return ("﻿" + s.replace("\n", "\r\n")).encode("utf-8")  # 메모장 호환(BOM, CRLF)


def _forbidden(rel: str) -> str | None:
    parts = rel.split("/")
    for d in parts[:-1]:
        if any(fnmatch.fnmatch(d, pat) for pat in FORBIDDEN_DIRS):
            return f"금지 폴더 {d}"
    if any(fnmatch.fnmatch(parts[-1], pat) for pat in FORBIDDEN_NAMES):
        return f"금지 파일 {parts[-1]}"
    return None


def build_zip(src: Path, top: str, readme: str, out: Path) -> dict:
    if not (src / f"{src.name}.exe").exists():
        raise SystemExit(f"빌드 결과가 없습니다: {src}")
    out.unlink(missing_ok=True)
    files = sorted(p for p in src.rglob("*") if p.is_file())
    problems = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            rel = p.relative_to(src).as_posix()
            if why := _forbidden(rel):
                problems.append(f"{rel}: {why}")
                continue
            data = p.read_bytes()
            if p.stat().st_size < 5_000_000 and any(b in data for b in FORBIDDEN_BYTES):
                problems.append(f"{rel}: 설정/비밀번호 흔적")
                continue
            z.write(p, f"{top}/{rel}")
        z.writestr(f"{top}/README.txt", _text(readme.format(version=__version__)))
    if problems:
        out.unlink(missing_ok=True)
        raise SystemExit("ZIP에 넣으면 안 되는 항목이 있습니다:\n" + "\n".join(problems))
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    return {"file": out.name, "bytes": out.stat().st_size, "sha256": sha, "files": len(files) + 1,
            "unpacked_bytes": sum(p.stat().st_size for p in files)}


def main() -> None:
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("ChromeJumper-*.zip"):
        old.unlink()
    user = build_zip(PYI / "ChromeJumper", "ChromeJumper", USER_README,
                     DIST / f"ChromeJumper-{__version__}-win64.zip")
    mock = build_zip(PYI / "MockSite", "ChromeJumper-MockSite", MOCK_README,
                     DIST / f"ChromeJumper-MockSite-{__version__}-win64.zip")
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "chrome_jumper", "packaging", "mock_site"],
                                    cwd=ROOT, capture_output=True, text=True).stdout.strip())
    except OSError:
        commit, dirty = "", False
    (DIST / "SHA256SUMS.txt").write_text(
        "".join(f"{x['sha256']}  {x['file']}\n" for x in (user, mock)), encoding="ascii")
    info = {"version": __version__, "built": datetime.now().isoformat(timespec="seconds"),
            "source_commit": commit, "source_dirty": dirty, "python": sys.version.split()[0],
            "artifacts": [user, mock]}
    (DIST / "BUILD_INFO.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    for x in (user, mock):
        print(f"{x['file']}: {x['bytes'] / 1e6:.1f} MB (풀면 {x['unpacked_bytes'] / 1e6:.1f} MB, {x['files']}개 파일)"
              f"\n  SHA-256 {x['sha256']}")


if __name__ == "__main__":
    main()
