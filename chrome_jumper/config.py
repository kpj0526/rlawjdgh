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

from .schedule import parse_hhmm

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

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("식별 이름을 입력하세요.")
        if not self.login_id.strip():
            raise ValueError("로그인 ID를 입력하세요.")
        if self.start_mode not in ("now", "at"):
            raise ValueError("시작 방식이 잘못되었습니다.")
        parse_hhmm(self.start_time)
        if self.end_time:
            parse_hhmm(self.end_time)
        if not (self.interval_min > 0):
            raise ValueError("반복 주기(분)는 0보다 커야 합니다.")


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
    cycle_timeout_sec: float = 180  # 한 주기 전체 제한

    # -------- 저장
    @classmethod
    def load(cls, path: Path) -> "Settings":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        accounts = [Account(**a) for a in raw.pop("accounts", [])]
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(accounts=accounts, **known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
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
