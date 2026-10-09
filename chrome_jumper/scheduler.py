"""계정별 예약 실행기. 별도 스레드의 asyncio 루프에서 동작하며 GUI와는 콜백으로만 통신한다."""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .config import Settings
from .runner import CycleResult, RunOptions, run_cycle
from .schedule import next_run

logger = logging.getLogger("chrome_jumper")

EventFn = Callable[[str, dict], None]


@dataclass
class AccountState:
    status: str = "정지"  # 정지 | 비활성 | 대기 | 실행 대기 | 실행 중 | 설정 오류
    next_run: datetime | None = None
    last_start: datetime | None = None
    last_end: datetime | None = None
    last_result: CycleResult | None = None
    history: list[CycleResult] = field(default_factory=list)


class Scheduler:
    def __init__(self, on_event: EventFn | None = None, run_fn=run_cycle):
        self.on_event = on_event or (lambda kind, data: None)
        self.run_fn = run_fn
        self.settings = Settings()
        self.states: dict[str, AccountState] = {}
        self.running = False  # 전체 시작 상태
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="scheduler", daemon=True)
        self._thread.start()
        self._loops: dict[str, asyncio.Task] = {}
        self._cycles: set[asyncio.Task] = set()
        self._changed: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0
        self._ctl = asyncio.Lock()  # 전체 시작/중지 직렬화(중지 진행 중 시작이 섞이지 않게)

    # ------------------------------------------------------------ 스레드 안전 공개 API
    # GUI(메인 스레드)는 스케줄러를 절대 동기로 기다리지 않는다(wait=None 기본).
    # 스케줄러 스레드가 Tk 객체 정리(예: GC가 부른 tkinter.Variable.__del__)로 메인 스레드를
    # 기다리는 순간 메인 스레드가 스케줄러를 기다리고 있으면 서로 기다리며 멈추기 때문이다.
    # 결과·오류는 on_event로 전달된다. wait 값은 테스트·스크립트용이다.
    def _call(self, coro, wait: float | None = None):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        fut.add_done_callback(self._report_failure)
        return fut.result(wait) if wait else fut

    def _report_failure(self, fut) -> None:
        if fut.cancelled() or fut.exception() is None:
            return
        exc = fut.exception()
        logger.error("스케줄러 명령 실패: %r", exc)
        self.on_event("log", {"time": datetime.now(), "account": "전체", "level": "ERROR",
                              "msg": f"스케줄러 명령 실패: {exc!r}"})

    def update_settings(self, settings: Settings, wait: float | None = None):
        return self._call(self._update(copy.deepcopy(settings)), wait)

    def start_all(self, wait: float | None = None):
        return self._call(self._start_all(), wait)

    def stop_all(self, wait: float | None = 30):
        """wait=None이면 기다리지 않고 Future를 돌려준다(GUI용)."""
        return self._call(self._stop_all(), wait)

    def run_now(self, acc_id: str, wait: float | None = None):
        return self._call(self._run_now(acc_id), wait)

    def close(self) -> None:
        """이벤트 루프 종료. 중지가 끝난 뒤 부른다."""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def shutdown(self) -> None:
        """중지 후 종료(테스트·스크립트용, 기다림). GUI는 stop_all(wait=None) 후 close()를 쓴다."""
        try:
            self.stop_all()
        finally:
            self.close()

    def snapshot(self) -> dict[str, AccountState]:
        return self._call(self._snapshot(), 10)

    # ------------------------------------------------------------ 내부(루프 스레드)
    def _log(self, acc_name: str, level: str, msg: str) -> None:
        getattr(logger, {"WARN": "warning"}.get(level, level.lower()))(f"[{acc_name}] {msg}")
        self.on_event("log", {"time": datetime.now(), "account": acc_name, "level": level, "msg": msg})

    def _emit(self, acc_id: str) -> None:
        self.on_event("state", {"id": acc_id, "state": copy.copy(self.states[acc_id])})

    async def _snapshot(self):
        return {k: copy.copy(v) for k, v in self.states.items()}

    def _state(self, acc_id: str) -> AccountState:
        if acc_id not in self.states:
            self.states[acc_id] = AccountState()
            self._changed[acc_id] = asyncio.Event()
            self._locks[acc_id] = asyncio.Lock()
        return self.states[acc_id]

    def _semaphore(self) -> asyncio.Semaphore:
        size = max(1, int(self.settings.max_concurrent))
        if self._sem is None or size != self._sem_size:
            # 크기 변경은 새 실행부터 적용
            self._sem, self._sem_size = asyncio.Semaphore(size), size
        return self._sem

    async def _update(self, settings: Settings) -> None:
        self.settings = settings
        ids = {a.id for a in settings.accounts}
        for acc in settings.accounts:
            self._state(acc.id)
            self._changed[acc.id].set()
        for gone in [k for k in self.states if k not in ids]:
            task = self._loops.pop(gone, None)
            if task:
                task.cancel()
            self.states.pop(gone)
            self.on_event("removed", {"id": gone})
        if self.running:
            for acc in settings.accounts:
                if acc.id not in self._loops:
                    self._loops[acc.id] = asyncio.create_task(self._account_loop(acc.id))

    async def _start_all(self) -> None:
        async with self._ctl:
            await self._start_all_locked()

    async def _start_all_locked(self) -> None:
        if self.running:
            return
        self.running = True
        self._log("전체", "INFO", "전체 시작")
        for acc in self.settings.accounts:
            self._state(acc.id)
            self._loops[acc.id] = asyncio.create_task(self._account_loop(acc.id))
        self.on_event("running", {"running": True})

    async def _stop_all(self) -> None:
        async with self._ctl:
            await self._stop_all_locked()

    async def _stop_all_locked(self) -> None:
        if self.running:
            self._log("전체", "INFO", "전체 중지 요청: 새 예약을 멈추고 진행 중 작업을 정리합니다.")
        self.running = False
        tasks = list(self._loops.values()) + list(self._cycles)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loops.clear()
        for acc_id, st in self.states.items():
            st.status, st.next_run = "정지", None
            self._emit(acc_id)
        self.on_event("running", {"running": False})
        if tasks:
            self._log("전체", "INFO", "전체 중지 완료")

    async def _run_now(self, acc_id: str) -> None:
        acc = self.settings.account(acc_id)
        if not acc:
            return
        self._state(acc_id)
        if self._locks[acc_id].locked():
            self._log(acc.name, "WARN", "이미 실행 중이라 '지금 실행'을 무시합니다.")
            return
        task = asyncio.create_task(self._run_one(acc_id, manual=True))
        self._cycles.add(task)
        task.add_done_callback(self._cycles.discard)

    async def _account_loop(self, acc_id: str) -> None:
        st = self._state(acc_id)
        changed = self._changed[acc_id]
        last_started: datetime | None = None
        while self.running:
            acc = self.settings.account(acc_id)
            if acc is None:
                return
            changed.clear()
            if not acc.enabled:
                st.status, st.next_run = "비활성", None
                self._emit(acc_id)
                await changed.wait()
                continue
            try:
                nxt = next_run(datetime.now(), start_mode=acc.start_mode, start_time=acc.start_time,
                               interval_min=acc.interval_min, end_time=acc.end_time or None,
                               last_started=last_started)
            except (ValueError, TypeError, OverflowError) as exc:  # 잘못된 값은 멈추지 말고 오류 표시
                st.status, st.next_run = "설정 오류", None
                self._emit(acc_id)
                self._log(acc.name, "ERROR", f"예약 계산 실패: {exc}")
                await changed.wait()
                continue
            if st.next_run != nxt or st.status != "대기":
                st.status, st.next_run = "대기", nxt
                self._emit(acc_id)
            delay = (nxt - datetime.now()).total_seconds()
            if delay > 0:
                try:
                    await asyncio.wait_for(changed.wait(), delay)
                    continue  # 설정이 바뀌어 다시 계산
                except asyncio.TimeoutError:
                    pass
                # Windows 타이머는 수 ms 일찍 깰 수 있으므로 예정 시각까지 마저 기다린다.
                while (rest := (nxt - datetime.now()).total_seconds()) > 0:
                    await asyncio.sleep(rest)
            if self._locks[acc_id].locked():
                self._log(acc.name, "WARN", "이전 실행이 아직 진행 중이라 이번 예약은 건너뜁니다.")
                last_started = datetime.now()
                continue
            last_started = datetime.now()
            await self._run_one(acc_id)
            took = (datetime.now() - last_started).total_seconds() / 60
            if took > acc.interval_min:
                self._log(acc.name, "WARN",
                          f"실행 시간({took:.1f}분)이 주기({acc.interval_min:g}분)보다 길어 지나간 예약은 건너뜁니다.")

    async def _run_one(self, acc_id: str, manual: bool = False) -> None:
        acc = self.settings.account(acc_id)
        st = self._state(acc_id)
        lock = self._locks[acc_id]
        if acc is None or lock.locked():
            return
        settings = self.settings
        async with lock:
            st.status = "실행 대기"
            self._emit(acc_id)
            async with self._semaphore():
                st.status, st.last_start = "실행 중", datetime.now()
                self._emit(acc_id)
                self._log(acc.name, "INFO", "실행 시작" + (" (지금 실행)" if manual else ""))
                opts = RunOptions(url=settings.target_url, jump_labels=list(settings.jump_labels),
                                  headless=settings.headless, chrome_path=settings.chrome_path,
                                  step_timeout_sec=settings.step_timeout_sec,
                                  dialog_timeout_sec=settings.dialog_timeout_sec,
                                  verify_timeout_sec=settings.verify_timeout_sec)
                result: CycleResult
                try:
                    if not opts.url:
                        raise ValueError("대상 URL이 설정되지 않았습니다.")
                    password = acc.password()
                    result = await asyncio.wait_for(
                        self.run_fn(opts, acc.login_id, password,
                                    lambda lvl, msg: self._log(acc.name, lvl, msg)),
                        settings.cycle_timeout_sec)
                except asyncio.TimeoutError:
                    result = CycleResult("실패", f"한 주기 시간 제한({settings.cycle_timeout_sec:g}초) 초과로 중단")
                    self._log(acc.name, "ERROR", result.message)
                except asyncio.CancelledError:
                    result = CycleResult("중지됨", "전체 중지로 진행 중 작업을 중단하고 Chrome을 닫았습니다.")
                    self._finish(acc_id, acc.name, result)
                    raise
                except Exception as exc:  # noqa: BLE001 - 계정 하나의 오류가 다른 계정을 멈추지 않게
                    result = CycleResult("실패", f"실행 오류: {exc}")
                    self._log(acc.name, "ERROR", result.message)
                self._finish(acc_id, acc.name, result)

    def _finish(self, acc_id: str, name: str, result: CycleResult) -> None:
        st = self.states.get(acc_id)
        if st is None:
            return
        result.finished = result.finished or datetime.now()
        st.last_end, st.last_result = result.finished, result
        st.history = (st.history + [result])[-20:]
        st.status = "대기" if self.running else "정지"
        self._log(name, "INFO" if result.status in ("성공", "건너뜀") else "WARN",
                  f"실행 종료: {result.status} - {result.message}")
        self._emit(acc_id)
