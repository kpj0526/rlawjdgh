"""한 계정의 한 주기: Chrome 열기 → URL 이동 → 로그인 → 점프 4종 클릭·확인 승인 → Chrome 닫기.

계정마다 Chrome을 따로 실행(임시 프로필)하므로 쿠키·로그인 상태가 섞이지 않는다.
중지는 asyncio 작업 취소로 전달되며, 어떤 경우에도 finally에서 자기 Chrome을 닫는다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from playwright.async_api import Error as PWError
from playwright.async_api import Page, async_playwright

LogFn = Callable[[str, str], None]  # (level, message)

# 점프 결과
OK = "성공"
COOLDOWN = "대기/비활성"
NO_BUTTON = "버튼 없음"
NO_DIALOG = "확인 창 없음"
FAILED = "실패"

SUCCESS_WORDS = ("완료", "되었습니다", "성공", "올렸습니다")
FAIL_WORDS = ("실패", "불가", "초과", "오류", "없습니다", "남은 시간", "후에 다시", "로그인")
COOLDOWN_RE = re.compile(r"대기|\d{1,2}:\d{2}")


class CycleError(Exception):
    """주기를 더 진행할 수 없는 오류(로그인 실패, Chrome 실행 실패 등)."""


@dataclass
class JumpResult:
    label: str
    outcome: str
    detail: str = ""


@dataclass
class CycleResult:
    status: str  # 성공 | 부분 성공 | 건너뜀 | 실패 | 중지됨
    message: str = ""
    jumps: list[JumpResult] = field(default_factory=list)
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None

    def summary(self) -> str:
        if not self.jumps:
            return self.message
        return " / ".join(f"{j.label.replace(' 점프', '')}:{j.outcome}" for j in self.jumps)


@dataclass
class RunOptions:
    url: str
    jump_labels: list[str]
    headless: bool = False
    chrome_path: str = ""
    step_timeout_sec: float = 20
    dialog_timeout_sec: float = 5


_FIND_JS = r"""
(label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const CLICK = 'button, a, input[type=button], input[type=submit], [role=button]';
  const textOf = el => norm(el.tagName === 'INPUT' ? el.value : el.innerText);
  document.querySelectorAll('[data-cj-target]').forEach(e => e.removeAttribute('data-cj-target'));
  const all = [...document.querySelectorAll(CLICK)].filter(visible);
  let hit = all.find(el => textOf(el) === label) || all.find(el => textOf(el).includes(label));
  let via = 'text';
  if (!hit) {
    // 쿨다운으로 버튼 글자가 바뀐 경우: 카드 제목에서 위로 올라가 버튼이 하나뿐인 영역을 찾는다.
    const titles = [...document.querySelectorAll('body *')].filter(el =>
      !el.closest(CLICK) && visible(el) &&
      ([...el.childNodes].some(n => n.nodeType === 3 && norm(n.textContent).includes(label)) ||
       (norm(el.innerText) === label)));
    for (const t of titles) {
      for (let c = t.parentElement; c && c !== document.body; c = c.parentElement) {
        const btns = [...c.querySelectorAll(CLICK)].filter(visible);
        if (btns.length === 1) { hit = btns[0]; via = 'card'; break; }
        if (btns.length > 1) break;
      }
      if (hit) break;
    }
  }
  if (!hit) return { found: false };
  hit.setAttribute('data-cj-target', '1');
  return {
    found: true, via, text: textOf(hit),
    disabled: !!(hit.disabled || hit.getAttribute('aria-disabled') === 'true' || hit.classList.contains('disabled')),
  };
}
"""

_LOGIN_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  document.querySelectorAll('[data-cj-id],[data-cj-pw],[data-cj-submit]').forEach(e => {
    e.removeAttribute('data-cj-id'); e.removeAttribute('data-cj-pw'); e.removeAttribute('data-cj-submit'); });
  const pw = [...document.querySelectorAll('input[type=password]')].find(visible);
  if (!pw) return { pw: false };
  const scope = pw.form || document;
  const texts = [...scope.querySelectorAll('input:not([type]), input[type=text], input[type=email], input[type=tel]')]
    .filter(visible);
  // 비밀번호 칸보다 앞에 있는 마지막 텍스트 입력칸 = 아이디 칸
  let id = null;
  for (const t of texts) {
    if (t.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING) id = t;
  }
  id = id || texts[0];
  pw.setAttribute('data-cj-pw', '1');
  if (id) id.setAttribute('data-cj-id', '1');
  const norm = s => (s || '').replace(/\s+/g, '').trim();
  const cands = [...scope.querySelectorAll('button, input[type=submit], input[type=button], a, [role=button]')].filter(visible);
  const sub = cands.find(el => norm(el.tagName === 'INPUT' ? el.value : el.innerText) === '로그인')
    || cands.find(el => el.type === 'submit')
    || cands.find(el => norm(el.tagName === 'INPUT' ? el.value : el.innerText).includes('로그인'));
  if (sub) sub.setAttribute('data-cj-submit', '1');
  return { pw: true, id: !!id, submit: !!sub };
}
"""

_CAPTCHA_JS = r"""
() => !!document.querySelector(
  'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], .g-recaptcha, .h-captcha, .cf-turnstile, img[src*="captcha" i], input[name*="captcha" i]'
) || /자동입력\s*방지|보안\s*문자/.test(document.body ? document.body.innerText : '')
"""

_HTML_CONFIRM_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  document.querySelectorAll('[data-cj-ok]').forEach(e => e.removeAttribute('data-cj-ok'));
  const btn = [...document.querySelectorAll(
    '[role=dialog] button, [role=alertdialog] button, .modal button, .swal2-popup button, dialog button')]
    .filter(visible).find(el => ['확인', 'OK', '예'].includes((el.innerText || '').trim()));
  if (!btn) return null;
  btn.setAttribute('data-cj-ok', '1');
  const box = btn.closest('[role=dialog],[role=alertdialog],.modal,.swal2-popup,dialog');
  return (box ? box.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 200);
}
"""


class _Session:
    def __init__(self, page: Page, opts: RunOptions, log: LogFn):
        self.page = page
        self.opts = opts
        self.log = log
        self.step_ms = opts.step_timeout_sec * 1000
        self.dialogs: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        page.on("dialog", self._on_dialog)

    async def _on_dialog(self, dialog) -> None:
        self.dialogs.put_nowait((dialog.type, dialog.message))
        try:
            await dialog.accept()  # 사이트 확인 창의 '확인'
        except PWError:
            pass

    def _drain(self) -> list[tuple[str, str]]:
        out = []
        while not self.dialogs.empty():
            out.append(self.dialogs.get_nowait())
        return out

    async def _next_dialog(self, timeout: float) -> tuple[str, str] | None:
        try:
            return await asyncio.wait_for(self.dialogs.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def ev(self, js: str, arg=None):
        """page.evaluate + 단계 시간 제한(이동 중 무한 대기 방지)."""
        try:
            return await asyncio.wait_for(self.page.evaluate(js, arg), self.opts.step_timeout_sec)
        except asyncio.TimeoutError:
            raise CycleError("페이지 응답 시간 초과") from None

    async def check_captcha(self) -> None:
        try:
            found = await asyncio.wait_for(self.page.evaluate(_CAPTCHA_JS), 3)
        except (PWError, asyncio.TimeoutError):
            return
        if found:
            raise CycleError("추가 인증(CAPTCHA 등)이 나타나 중단했습니다. 자동으로 우회하지 않습니다.")

    async def goto(self) -> None:
        try:
            await self.page.goto(self.opts.url, wait_until="domcontentloaded", timeout=self.step_ms)
        except PWError as exc:
            raise CycleError(f"페이지 열기 실패/시간 초과: {_short(exc)}") from exc

    async def _jumps_visible(self) -> bool:
        for label in self.opts.jump_labels:
            if (await self.ev(_FIND_JS, label)).get("found"):
                return True
        return False

    async def login(self, login_id: str, password: str) -> None:
        info = await self.ev(_LOGIN_JS)
        if not info["pw"]:
            if await self._jumps_visible():
                self.log("INFO", "로그인 폼 없이 점프 화면이 열려 로그인 단계를 생략합니다.")
                return
            # 로그인 링크를 눌러 폼을 연다.
            link = self.page.get_by_role("link", name="로그인").or_(self.page.get_by_role("button", name="로그인"))
            if await link.count():
                await link.first.click(timeout=self.step_ms)
                await self.page.wait_for_load_state("domcontentloaded", timeout=self.step_ms)
                try:
                    await self.page.locator("input[type=password]").first.wait_for(
                        state="visible", timeout=self.step_ms)
                except PWError:
                    pass
                info = await self.ev(_LOGIN_JS)
            if not info["pw"]:
                await self.check_captcha()
                raise CycleError("로그인 폼(비밀번호 입력칸)을 찾지 못했습니다.")
        if not info["id"]:
            raise CycleError("아이디 입력칸을 찾지 못했습니다.")
        await self.check_captcha()

        self._drain()
        await self.page.fill("[data-cj-id]", login_id, timeout=self.step_ms)
        await self.page.fill("[data-cj-pw]", password, timeout=self.step_ms)
        try:
            if info["submit"]:
                await self.page.click("[data-cj-submit]", timeout=self.step_ms, no_wait_after=True)
            else:
                await self.page.press("[data-cj-pw]", "Enter", timeout=self.step_ms, no_wait_after=True)
        except PWError as exc:
            raise CycleError(f"로그인 버튼 클릭 실패: {_short(exc)}") from exc

        # 결과 대기: 비밀번호 칸이 사라지면 성공, 알림 창이 뜨고 칸이 남아 있으면 실패.
        # 페이지 이동 중에는 개별 호출이 멈출 수 있으므로 단계 전체에 시간 제한을 건다.
        msgs: list[str] = []
        try:
            await asyncio.wait_for(self._await_login(msgs), self.opts.step_timeout_sec)
        except asyncio.TimeoutError:
            await self.check_captcha()
            raise CycleError("로그인 실패: 시간 안에 로그인 화면을 벗어나지 못했습니다(응답 지연)." +
                             (f" ({msgs[-1]})" if msgs else "")) from None

    async def _await_login(self, msgs: list[str]) -> None:
        while True:
            msgs.extend(m for _t, m in self._drain())
            try:
                still = await self.page.locator("input[type=password]:visible").count()
            except PWError:
                still = 1  # 페이지 전환 중
            if not still:
                try:
                    await self.page.wait_for_load_state("domcontentloaded")
                except PWError:
                    pass
                return
            if msgs:
                await asyncio.sleep(0.5)
                if await self.page.locator("input[type=password]:visible").count():
                    raise CycleError(f"로그인 실패: {msgs[-1]}")
            await asyncio.sleep(0.25)

    async def open_jump_page(self) -> None:
        """로그인 뒤 점프 버튼이 있는 화면을 확보한다. 없으면 설정 URL로 다시 이동."""
        if await self._wait_jumps(self.opts.step_timeout_sec / 2):
            return
        self.log("INFO", "로그인 후 점프 화면이 아니어서 설정 URL로 다시 이동합니다.")
        await self.goto()
        await self.check_captcha()
        if await self.page.locator("input[type=password]:visible").count():
            raise CycleError("로그인 상태가 유지되지 않았습니다(다시 로그인 화면).")
        if not await self._wait_jumps(self.opts.step_timeout_sec):
            raise CycleError("점프 버튼이 있는 화면을 찾지 못했습니다. 대상 URL을 확인하세요.")

    async def _wait_jumps(self, timeout: float) -> bool:
        async def poll():
            while True:
                try:
                    if await self._jumps_visible():
                        return
                except (PWError, CycleError):
                    pass  # 이동 중
                await asyncio.sleep(0.3)

        try:
            await asyncio.wait_for(poll(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def jump(self, label: str) -> JumpResult:
        try:
            found = await self.ev(_FIND_JS, label)
        except (PWError, CycleError) as exc:
            return JumpResult(label, FAILED, f"페이지 오류: {_short(exc)}")
        if not found["found"]:
            return JumpResult(label, NO_BUTTON, "버튼을 찾지 못했습니다.")
        text = found["text"]
        if found["disabled"] or (found["via"] == "card" and COOLDOWN_RE.search(text)):
            return JumpResult(label, COOLDOWN, f"버튼 상태: {text or '비활성'}")
        if found["via"] == "card":
            return JumpResult(label, NO_BUTTON, f"이름이 다른 버튼만 있습니다: {text}")

        self._drain()
        try:
            await self.page.click("[data-cj-target]", timeout=self.step_ms, no_wait_after=True)
        except PWError as exc:
            return JumpResult(label, FAILED, f"클릭 실패: {_short(exc)}")

        first = await self._next_dialog(self.opts.dialog_timeout_sec)
        if first is None:
            # 브라우저 기본 창이 아닌 HTML 모달 확인 창 대응
            try:
                modal = await self.ev(_HTML_CONFIRM_JS)
                if modal is not None:
                    await self.page.click("[data-cj-ok]", timeout=self.step_ms)
                    first = ("modal", modal)
            except (PWError, CycleError):
                pass
        if first is None:
            return JumpResult(label, NO_DIALOG, f"{self.opts.dialog_timeout_sec:g}초 안에 확인 창이 나타나지 않았습니다.")

        typ, msg = first
        if typ == "alert" and _is_failure(msg):
            return JumpResult(label, FAILED, f"사이트 알림: {msg}")

        # 확인 뒤 결과 알림(있을 수 있음)을 잠깐 기다린다.
        follow = await self._next_dialog(min(1.5, self.opts.dialog_timeout_sec))
        if follow and _is_failure(follow[1]):
            return JumpResult(label, FAILED, f"확인 후 사이트 알림: {follow[1]}")
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=self.step_ms)
        except PWError:
            pass
        detail = f"확인 승인: {msg}"
        if follow:
            detail += f" → {follow[1]}"
        return JumpResult(label, OK, detail)


def _is_failure(msg: str) -> bool:
    if any(w in msg for w in SUCCESS_WORDS):
        return False
    return any(w in msg for w in FAIL_WORDS)


def _short(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__


def _overall(jumps: list[JumpResult]) -> str:
    ok = sum(j.outcome == OK for j in jumps)
    if ok == len(jumps):
        return "성공"
    if ok:
        return "부분 성공"
    if all(j.outcome == COOLDOWN for j in jumps):
        return "건너뜀"
    return "실패"


async def run_cycle(opts: RunOptions, login_id: str, password: str, log: LogFn) -> CycleResult:
    result = CycleResult(status="실패")
    pw = browser = None
    try:
        pw = await async_playwright().start()
        launch = dict(headless=opts.headless, timeout=opts.step_timeout_sec * 1000,
                      args=["--no-first-run", "--no-default-browser-check", "--disable-sync"])
        if opts.chrome_path:
            launch["executable_path"] = opts.chrome_path
        else:
            launch["channel"] = "chrome"
        try:
            browser = await pw.chromium.launch(**launch)
        except PWError as exc:
            raise CycleError(f"Chrome 실행 실패: {_short(exc)}") from exc
        log("INFO", "Chrome 실행(독립 세션)")
        context = await browser.new_context(locale="ko-KR")
        page = await context.new_page()
        s = _Session(page, opts, log)

        await s.goto()
        await s.check_captcha()
        await s.login(login_id, password)
        log("INFO", "로그인 성공")
        await s.open_jump_page()

        for label in opts.jump_labels:
            jr = await s.jump(label)
            result.jumps.append(jr)
            log("INFO" if jr.outcome == OK else "WARN", f"{label}: {jr.outcome} - {jr.detail}")
        result.status = _overall(result.jumps)
        result.message = result.summary()
    except asyncio.CancelledError:
        result.status = "중지됨"
        result.message = "사용자 중지 또는 시간 제한으로 중단"
        raise
    except CycleError as exc:
        result.message = str(exc)
        log("ERROR", str(exc))
    except PWError as exc:
        result.message = f"브라우저 오류: {_short(exc)}"
        log("ERROR", result.message)
    finally:
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), 15)
                log("INFO", "Chrome 닫음")
            except Exception as exc:  # noqa: BLE001
                log("ERROR", f"Chrome 닫기 실패: {_short(exc)}")
        if pw is not None:
            try:
                await asyncio.wait_for(pw.stop(), 15)
            except Exception:  # noqa: BLE001
                pass
        result.finished = datetime.now()
    return result
