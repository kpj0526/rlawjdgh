"""설정 저장/불러오기. 비밀번호는 Windows DPAPI(현재 Windows 사용자 전용)로 암호화해 저장한다."""

from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import math

from .schedule import INTERVAL_USER_MIN, check_interval, parse_hhmm

# 시간 제한 허용 범위(초): 단계, 확인 창, 한 주기, 결과 확인 순서
TIMEOUT_RANGES = ((1, 600), (1, 120), (10, 3600), (1, 120))
# 점프 간격 허용 범위(초): 클릭 간격, 확인 승인 지연. 0이면 간격 없음(권장하지 않음).
PACE_RANGE = (0, 10)

DEFAULT_JUMPS = ["라인업/PR 점프", "실시간 출근부 점프", "매니저 출근부 점프", "홍보관 점프"]


def default_data_dir() -> Path:
    override = os.environ.get("CHROME_JUMPER_DATA")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ChromeJumper"


# ---------------------------------------------------------------- 비밀번호 보호

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: _Blob) -> bytes:
        out = ctypes.string_at(blob.pbData, blob.cbData)
        ctypes.windll.kernel32.LocalFree(blob.pbData)
        return out

    def protect(plain: str) -> str:
        if not plain:
            return ""
        src, dst = _to_blob(plain.encode("utf-8")), _Blob()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(src), "ChromeJumper", None, None, None, 0x01, ctypes.byref(dst)
        ):
            raise OSError("비밀번호 암호화(DPAPI) 실패")
        return "dpapi:" + base64.b64encode(_from_blob(dst)).decode()

    def unprotect(token: str) -> str:
        if not token:
            return ""
        if not token.startswith("dpapi:"):
            raise ValueError("알 수 없는 비밀번호 저장 형식")
        src, dst = _to_blob(base64.b64decode(token[6:])), _Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)
        ):
            raise OSError("비밀번호 복호화 실패(다른 Windows 사용자/PC에서 만든 설정일 수 있음)")
        return _from_blob(dst).decode("utf-8")

else:  # 개발용 비Windows 대체(암호화 아님, 단순 인코딩)

    def protect(plain: str) -> str:
        return "b64:" + base64.b64encode(plain.encode()).decode() if plain else ""

    def unprotect(token: str) -> str:
        return base64.b64decode(token[4:]).decode() if token else ""


# ---------------------------------------------------------------- 데이터 모델


@dataclass
class Account:
    name: str
    login_id: str
    password_enc: str = ""
    enabled: bool = True
    start_mode: str = "at"  # "now" | "at"
    start_time: str = "09:00"
    interval_min: float = 60
    end_time: str = ""  # 선택. 비우면 종료 시각 없음
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def set_password(self, plain: str) -> None:
        self.password_enc = protect(plain)

    def password(self) -> str:
        return unprotect(self.password_enc)

    def sanitize(self) -> list[str]:
        """설정 파일의 잘못된 값을 기본값으로 바꾼다(저장 가능한 상태로). 바꾼 항목 이름을 돌려준다."""
        fixed = []
        default = Account(name="", login_id="")
        for attr, check in (("interval_min", lambda v: check_interval(v, INTERVAL_USER_MIN)),
                            ("start_time", parse_hhmm),
                            ("end_time", lambda v: v == "" or parse_hhmm(v))):
            try:
                check(getattr(self, attr))
            except ValueError:
                setattr(self, attr, getattr(default, attr))
                fixed.append({"interval_min": "반복 주기", "start_time": "시작 시각", "end_time": "종료 시각"}[attr])
        if self.start_mode not in ("now", "at"):
            self.start_mode = default.start_mode
            fixed.append("시작 방식")
        for attr in ("name", "login_id", "password_enc"):
            if not isinstance(getattr(self, attr), str):
                setattr(self, attr, str(getattr(self, attr)))
        if not isinstance(self.enabled, bool):
            self.enabled = False
        return fixed

    def validate(self) -> None:
        """화면 입력·설정 파일 로드 공통 검사. 실행 가능한 값만 통과한다."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("식별 이름을 입력하세요.")
        if not isinstance(self.login_id, str) or not self.login_id.strip():
            raise ValueError("로그인 ID를 입력하세요.")
        if not isinstance(self.enabled, bool):
            raise ValueError("활성 여부 값이 잘못되었습니다.")
        if self.start_mode not in ("now", "at"):
            raise ValueError("시작 방식이 잘못되었습니다.")
        parse_hhmm(self.start_time)
        if self.end_time:
            parse_hhmm(self.end_time)
        self.interval_min = check_interval(self.interval_min, INTERVAL_USER_MIN)


@dataclass
class Settings:
    target_url: str = ""
    accounts: list[Account] = field(default_factory=list)
    jump_labels: list[str] = field(default_factory=lambda: list(DEFAULT_JUMPS))
    headless: bool = False
    chrome_path: str = ""  # 비우면 설치된 Chrome 자동 사용
    max_concurrent: int = 4
    step_timeout_sec: float = 20  # 페이지 이동·요소 대기 제한
    dialog_timeout_sec: float = 5  # 버튼 클릭 뒤 확인 창 대기 제한
    verify_timeout_sec: float = 8  # 확인 승인 뒤 사이트 성공 신호 대기 제한
    cycle_timeout_sec: float = 180  # 한 주기 전체 제한
    # 점프 간격(요청 005). 실제 사이트가 빠른 연속 조작을 거부하면 늘린다.
    click_gap_sec: float = 0.5  # 점프 화면 표시·직전 점프 판정 뒤 다음 버튼 클릭까지
    confirm_delay_sec: float = 0.4  # 확인 창이 뜬 뒤 '확인' 승인까지

    # -------- 저장
    # 설정 파일을 읽을 때 발견한 문제(화면에 알림). 저장하지 않는다.
    load_errors: list[str] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """설정 파일 읽기. 잘못된 계정 값은 그 계정을 비활성으로 돌리고 load_errors에 적는다."""
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        errors: list[str] = []
        accounts = []
        for i, a in enumerate(raw.pop("accounts", []) or []):
            try:
                acc = Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__})
            except TypeError as exc:
                errors.append(f"{i + 1}번째 계정을 읽지 못해 제외했습니다: {exc}")
                continue
            try:
                acc.validate()
            except ValueError as exc:
                fixed = acc.sanitize()
                acc.enabled = False
                note = f" ({', '.join(fixed)} 기본값으로 바꿈)" if fixed else ""
                errors.append(f"계정 '{acc.name}': {exc}{note} → 비활성으로 바꿨습니다. 확인·수정 후 다시 활성화하세요.")
            accounts.append(acc)
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and k != "load_errors"}
        s = cls(accounts=accounts, **known)
        s.load_errors = errors + s._fix_options()
        return s

    def _fix_options(self) -> list[str]:
        """전역 옵션이 실행 불가능한 값이면 기본값으로 되돌린다."""
        errors = []
        default = Settings()
        names = ("step_timeout_sec", "dialog_timeout_sec", "cycle_timeout_sec", "verify_timeout_sec")
        for name, (lo, hi) in zip(names, TIMEOUT_RANGES):
            v = getattr(self, name)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi
            if not ok:
                errors.append(f"옵션 {name}={v!r} 이(가) 잘못되어 기본값 {getattr(default, name)}(으)로 바꿨습니다.")
                setattr(self, name, getattr(default, name))
        lo, hi = PACE_RANGE
        for name in ("click_gap_sec", "confirm_delay_sec"):
            v = getattr(self, name)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi
            if not ok:
                errors.append(f"옵션 {name}={v!r} 이(가) 잘못되어 기본값 {getattr(default, name)}(으)로 바꿨습니다.")
                setattr(self, name, getattr(default, name))
        mc = self.max_concurrent
        if isinstance(mc, bool) or not isinstance(mc, int) or not 1 <= mc <= 20:
            errors.append(f"옵션 max_concurrent={mc!r} 이(가) 잘못되어 기본값 4로 바꿨습니다.")
            self.max_concurrent = 4
        if not isinstance(self.target_url, str):
            errors.append("대상 URL 값이 잘못되어 비웠습니다.")
            self.target_url = ""
        if not isinstance(self.jump_labels, list) or not all(isinstance(x, str) and x.strip() for x in self.jump_labels) \
                or not self.jump_labels:
            errors.append("점프 버튼 이름 목록이 잘못되어 기본값으로 바꿨습니다.")
            self.jump_labels = list(DEFAULT_JUMPS)
        return errors

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        data = asdict(self)
        data.pop("load_errors", None)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(tmp, path)

    def account(self, acc_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == acc_id), None)


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("대상 URL은 http:// 또는 https:// 로 시작하는 전체 주소여야 합니다.")
    if " " in url:
        raise ValueError("URL에 공백이 있습니다.")
    return url
