"""tkinter GUI."""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .config import Account, Settings, default_data_dir, validate_url
from .scheduler import AccountState, Scheduler

BG, PANEL, FG, MUTED, ACCENT = "#1d1f24", "#262931", "#e7e9ee", "#9aa1ad", "#3d7eff"
STATUS_COLORS = {
    "실행 중": "#4da3ff", "실행 대기": "#4da3ff", "대기": "#e7e9ee", "정지": "#9aa1ad", "비활성": "#6b7280",
    "설정 오류": "#ff6b6b",
}
RESULT_COLORS = {"성공": "#3ecf8e", "부분 성공": "#f5b942", "건너뜀": "#c9a227", "실패": "#ff6b6b", "중지됨": "#9aa1ad"}
LEVEL_COLORS = {"INFO": FG, "WARN": "#f5b942", "ERROR": "#ff6b6b"}


def fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%H:%M:%S") if dt.date() == datetime.now().date() else dt.strftime("%m-%d %H:%M")


def setup_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(log_dir / "chrome_jumper.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    lg = logging.getLogger("chrome_jumper")
    lg.setLevel(logging.INFO)
    lg.addHandler(h)


class AccountDialog(tk.Toplevel):
    def __init__(self, master, account: Account | None):
        super().__init__(master)
        self.title("계정 수정" if account else "계정 추가")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.result: tuple[Account, str | None] | None = None
        self.account = account
        a = account or Account(name="", login_id="")

        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        self.v_name = tk.StringVar(value=a.name)
        self.v_id = tk.StringVar(value=a.login_id)
        self.v_pw = tk.StringVar()
        self.v_enabled = tk.BooleanVar(value=a.enabled)
        self.v_mode = tk.StringVar(value=a.start_mode)
        self.v_start = tk.StringVar(value=a.start_time)
        self.v_interval = tk.StringVar(value=f"{a.interval_min:g}")
        self.v_end = tk.StringVar(value=a.end_time)

        r = 0

        def row(label, widget, hint=""):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="we", pady=4)
            if hint:
                ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=8)
            r += 1

        row("식별 이름", ttk.Entry(f, textvariable=self.v_name, width=28))
        row("로그인 ID", ttk.Entry(f, textvariable=self.v_id, width=28))
        row("비밀번호", ttk.Entry(f, textvariable=self.v_pw, show="●", width=28),
            "수정 시 비우면 기존 비밀번호 유지" if account else "")
        row("사용", ttk.Checkbutton(f, text="활성 (전체 시작 시 예약)", variable=self.v_enabled))
        modes = ttk.Frame(f)
        ttk.Radiobutton(modes, text="즉시 시작 (전체 시작 즉시 1회, 이후 주기마다)",
                        variable=self.v_mode, value="now").pack(anchor="w")
        ttk.Radiobutton(modes, text="지정 시각에 시작 (매일 시작 시각 기준 주기 격자)",
                        variable=self.v_mode, value="at").pack(anchor="w")
        row("첫 실행", modes)
        self.e_start = ttk.Entry(f, textvariable=self.v_start, width=8)
        row("시작 시각", self.e_start, "HH:MM (24시간). 종료 시각이 있으면 매일 이 시각에 재개")
        row("반복 주기(분)", ttk.Entry(f, textvariable=self.v_interval, width=8), "예: 10, 30, 60")
        self.e_end = ttk.Entry(f, textvariable=self.v_end, width=8)
        row("종료 시각", self.e_end, "선택. 비우면 전체 중지까지 반복, 입력하면 매일 이 시각까지만")

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="취소", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="저장", style="Accent.TButton", command=self._ok).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _ok(self):
        try:
            try:
                interval = float(self.v_interval.get())
            except ValueError:
                raise ValueError("반복 주기(분)는 숫자여야 합니다.") from None
            base = self.account
            acc = Account(
                name=self.v_name.get().strip(), login_id=self.v_id.get().strip(),
                password_enc=base.password_enc if base else "", enabled=self.v_enabled.get(),
                start_mode=self.v_mode.get(), start_time=self.v_start.get().strip() or "09:00",
                interval_min=interval, end_time=self.v_end.get().strip(),
                **({"id": base.id} if base else {}),
            )
            acc.validate()
            pw = self.v_pw.get()
            if not base and not pw:
                raise ValueError("비밀번호를 입력하세요.")
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc), parent=self)
            return
        self.result = (acc, pw or None)
        self.destroy()


class AdvancedDialog(tk.Toplevel):
    def __init__(self, master, s: Settings):
        super().__init__(master)
        self.title("고급 설정")
        self.configure(bg=BG)
        self.transient(master)
        self.ok = False
        self.s = s
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        self.v_chrome = tk.StringVar(value=s.chrome_path)
        self.v_step = tk.StringVar(value=f"{s.step_timeout_sec:g}")
        self.v_dialog = tk.StringVar(value=f"{s.dialog_timeout_sec:g}")
        self.v_cycle = tk.StringVar(value=f"{s.cycle_timeout_sec:g}")
        items = [
            ("Chrome 실행 파일", self.v_chrome, "비우면 설치된 Chrome 자동 사용", 40),
            ("단계 대기 제한(초)", self.v_step, "페이지 이동·로그인·버튼 대기", 8),
            ("확인 창 대기(초)", self.v_dialog, "버튼 클릭 뒤 확인 창", 8),
            ("한 주기 제한(초)", self.v_cycle, "넘으면 실패 처리 후 Chrome 닫음", 8),
        ]
        for i, (lab, var, hint, w) in enumerate(items):
            ttk.Label(f, text=lab).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var, width=w).grid(row=i, column=1, sticky="w", pady=4, padx=8)
            ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky="w")
        ttk.Label(f, text="점프 버튼 이름\n(한 줄에 하나, 순서대로 클릭)").grid(row=4, column=0, sticky="nw", pady=4)
        self.t_labels = tk.Text(f, width=30, height=5, bg=PANEL, fg=FG, insertbackground=FG, relief="flat")
        self.t_labels.insert("1.0", "\n".join(s.jump_labels))
        self.t_labels.grid(row=4, column=1, columnspan=2, sticky="w", pady=4, padx=8)
        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(b, text="취소", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="저장", style="Accent.TButton", command=self._ok).pack(side="right")
        self.grab_set()

    def _ok(self):
        try:
            vals = [float(v.get()) for v in (self.v_step, self.v_dialog, self.v_cycle)]
            if min(vals) <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("입력 오류", "시간 값은 0보다 큰 숫자여야 합니다.", parent=self)
            return
        labels = [x.strip() for x in self.t_labels.get("1.0", "end").splitlines() if x.strip()]
        if not labels:
            messagebox.showerror("입력 오류", "점프 버튼 이름을 하나 이상 입력하세요.", parent=self)
            return
        chrome = self.v_chrome.get().strip().strip('"')
        if chrome and not Path(chrome).is_file():
            messagebox.showerror("입력 오류", "Chrome 실행 파일 경로가 없습니다.", parent=self)
            return
        self.s.chrome_path = chrome
        self.s.step_timeout_sec, self.s.dialog_timeout_sec, self.s.cycle_timeout_sec = vals
        self.s.jump_labels = labels
        self.ok = True
        self.destroy()


class App:
    COLS = ("name", "login", "enabled", "mode", "interval", "end", "status", "last", "result", "next")
    HEAD = ("이름", "로그인 ID", "활성", "첫 실행", "주기(분)", "종료", "상태", "마지막 실행", "마지막 결과", "다음 실행")
    WIDTH = (110, 100, 50, 110, 70, 60, 80, 100, 100, 100)

    def __init__(self, root: tk.Tk, data_dir: Path):
        self.root = root
        self.data_dir = data_dir
        self.cfg_path = data_dir / "config.json"
        try:
            self.settings = Settings.load(self.cfg_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("설정 오류", f"설정 파일을 읽지 못했습니다: {exc}\n빈 설정으로 시작합니다.")
            self.settings = Settings()
        self.states: dict[str, AccountState] = {}
        self.events: queue.Queue = queue.Queue()
        self.scheduler = Scheduler(on_event=lambda k, d: self.events.put((k, d)))
        self.scheduler.update_settings(self.settings)
        self._style()
        self._build()
        self._refresh_table()
        self._poll()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI 구성
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        font = ("Malgun Gothic", 10)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", font)
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=font, bordercolor="#3a3f4b")
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Title.TLabel", font=("Malgun Gothic", 15, "bold"))
        st.configure("TButton", background="#343844", foreground=FG, padding=(10, 5), borderwidth=0)
        st.map("TButton", background=[("active", "#414654"), ("disabled", "#2a2d35")],
               foreground=[("disabled", "#5d6370")])
        st.configure("Accent.TButton", background=ACCENT, foreground="white")
        st.map("Accent.TButton", background=[("active", "#5b93ff"), ("disabled", "#2a3550")])
        st.configure("Stop.TButton", background="#c2453f", foreground="white")
        st.map("Stop.TButton", background=[("active", "#d85a54"), ("disabled", "#45292a")])
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.map("TRadiobutton", background=[("active", BG)])
        st.configure("TSpinbox", fieldbackground=PANEL, foreground=FG, arrowcolor=FG)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background="#30343e", foreground=MUTED, relief="flat")
        st.map("Treeview", background=[("selected", "#34446b")])
        st.configure("TLabelframe", background=BG, bordercolor="#3a3f4b")
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED)

    def _build(self):
        r = self.root
        r.title(f"Chrome 점프 자동화 v{__version__}")
        r.geometry("1180x820")
        r.minsize(980, 620)

        top = ttk.Frame(r, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Chrome 점프 자동화", style="Title.TLabel").pack(side="left")
        self.l_run = tk.Label(top, text="● 정지", bg=BG, fg=MUTED, font=("Malgun Gothic", 11, "bold"))
        self.l_run.pack(side="left", padx=14)
        self.b_stop = ttk.Button(top, text="■ 전체 중지", style="Stop.TButton", command=self._stop_all)
        self.b_stop.pack(side="right")
        self.b_start = ttk.Button(top, text="▶ 전체 시작", style="Accent.TButton", command=self._start_all)
        self.b_start.pack(side="right", padx=6)

        cfg = ttk.Frame(r, padding=(16, 4))
        cfg.pack(fill="x")
        ttk.Label(cfg, text="대상 URL").pack(side="left")
        self.v_url = tk.StringVar(value=self.settings.target_url)
        e = ttk.Entry(cfg, textvariable=self.v_url, width=60)
        e.pack(side="left", padx=8, fill="x", expand=True)
        e.bind("<Return>", lambda ev: self._apply_url())
        ttk.Button(cfg, text="URL 적용", command=self._apply_url).pack(side="left")
        ttk.Label(cfg, text="  동시 실행").pack(side="left")
        self.v_conc = tk.StringVar(value=str(self.settings.max_concurrent))
        sp = ttk.Spinbox(cfg, from_=1, to=20, width=4, textvariable=self.v_conc, command=self._apply_misc)
        sp.pack(side="left", padx=4)
        sp.bind("<FocusOut>", lambda ev: self._apply_misc())
        self.v_headless = tk.BooleanVar(value=self.settings.headless)
        ttk.Checkbutton(cfg, text="Chrome 창 숨기기", variable=self.v_headless,
                        command=self._apply_misc).pack(side="left", padx=8)
        ttk.Button(cfg, text="고급 설정", command=self._advanced).pack(side="left")
        self.l_url = ttk.Label(r, text=self._url_hint(), style="Muted.TLabel", padding=(16, 0))
        self.l_url.pack(fill="x")

        pw = ttk.PanedWindow(r, orient="vertical")
        pw.pack(fill="both", expand=True, padx=16, pady=8)

        upper = ttk.Frame(pw)
        bar = ttk.Frame(upper)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="계정").pack(side="left")
        for text, cmd in (("지금 실행", self._run_now), ("삭제", self._delete), ("수정", self._edit),
                          ("계정 추가", self._add)):
            ttk.Button(bar, text=text, command=cmd).pack(side="right", padx=3)
        det = ttk.LabelFrame(upper, text="선택 계정 - 최근 실행의 버튼별 결과", padding=6)
        det.pack(side="bottom", fill="x", pady=(8, 0))
        self.l_det = ttk.Label(det, text="계정을 선택하세요.", style="Muted.TLabel")
        self.l_det.pack(fill="x")
        self.tv_det = ttk.Treeview(det, columns=("btn", "out", "detail"), show="headings", height=4)
        for c, h, w in (("btn", "버튼", 150), ("out", "결과", 100), ("detail", "상세/오류 원인", 700)):
            self.tv_det.heading(c, text=h)
            self.tv_det.column(c, width=w, anchor="w")
        for k, v in {"성공": "#3ecf8e", "대기/비활성": "#c9a227", "버튼 없음": "#ff6b6b",
                     "확인 창 없음": "#ff6b6b", "실패": "#ff6b6b"}.items():
            self.tv_det.tag_configure(k, foreground=v)
        self.tv_det.pack(fill="x")
        tv = ttk.Treeview(upper, columns=self.COLS, show="headings", selectmode="browse", height=6)
        for c, h, w in zip(self.COLS, self.HEAD, self.WIDTH):
            tv.heading(c, text=h)
            tv.column(c, width=w, anchor="center" if c not in ("name", "login") else "w")
        for k, v in {**STATUS_COLORS, **RESULT_COLORS}.items():
            tv.tag_configure(k, foreground=v)
        tv.pack(fill="both", expand=True)
        tv.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        tv.bind("<Double-1>", lambda e: self._edit())
        self.tv = tv

        pw.add(upper, weight=3)

        lower = ttk.LabelFrame(pw, text="실행 로그", padding=6)
        self.t_log = ScrolledText(lower, height=10, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                                  font=("Consolas", 9), state="disabled")
        for k, v in LEVEL_COLORS.items():
            self.t_log.tag_configure(k, foreground=v)
        self.t_log.pack(fill="both", expand=True)
        pw.add(lower, weight=2)
        self._set_running(False)

    def _url_hint(self) -> str:
        u = self.settings.target_url
        return f"현재 적용된 URL: {u}" if u else "대상 URL이 비어 있습니다. 로그인 화면 또는 업소 정보 화면 주소를 입력하세요."

    # ------------------------------------------------------------------ 설정 변경
    def _save(self):
        self.settings.save(self.cfg_path)
        self.scheduler.update_settings(self.settings)

    def _apply_url(self):
        try:
            self.settings.target_url = validate_url(self.v_url.get())
        except ValueError as exc:
            messagebox.showerror("URL 오류", str(exc))
            return
        self._save()
        self.l_url.configure(text=self._url_hint())
        self._log_line(datetime.now(), "설정", "INFO", f"대상 URL 적용: {self.settings.target_url} (다음 실행부터)")

    def _apply_misc(self):
        try:
            n = int(self.v_conc.get())
            if not 1 <= n <= 20:
                raise ValueError
        except ValueError:
            self.v_conc.set(str(self.settings.max_concurrent))
            return
        if n == self.settings.max_concurrent and self.v_headless.get() == self.settings.headless:
            return
        self.settings.max_concurrent = n
        self.settings.headless = self.v_headless.get()
        self._save()

    def _advanced(self):
        d = AdvancedDialog(self.root, self.settings)
        self.root.wait_window(d)
        if d.ok:
            self._save()

    def _selected(self) -> Account | None:
        sel = self.tv.selection()
        return self.settings.account(sel[0]) if sel else None

    def _add(self):
        d = AccountDialog(self.root, None)
        self.root.wait_window(d)
        if d.result:
            acc, pw = d.result
            acc.set_password(pw or "")
            self.settings.accounts.append(acc)
            self._save()
            self._refresh_table()
            self._log_line(datetime.now(), acc.name, "INFO", "계정 추가")

    def _edit(self):
        acc = self._selected()
        if not acc:
            return
        d = AccountDialog(self.root, acc)
        self.root.wait_window(d)
        if d.result:
            new, pw = d.result
            if pw:
                new.set_password(pw)
            idx = self.settings.accounts.index(acc)
            self.settings.accounts[idx] = new
            self._save()
            self._refresh_table()
            self._log_line(datetime.now(), new.name, "INFO", "계정 설정 변경 (다음 실행 시각 다시 계산)")

    def _delete(self):
        acc = self._selected()
        if not acc:
            return
        if not messagebox.askyesno("계정 삭제", f"'{acc.name}' 계정을 삭제할까요?\n실행 중이면 중단하고 Chrome을 닫습니다."):
            return
        self.settings.accounts.remove(acc)
        self.states.pop(acc.id, None)
        self._save()
        self._refresh_table()
        self._log_line(datetime.now(), acc.name, "INFO", "계정 삭제")

    def _run_now(self):
        acc = self._selected()
        if not acc:
            messagebox.showinfo("지금 실행", "실행할 계정을 선택하세요.")
            return
        if not self._check_ready():
            return
        self.scheduler.run_now(acc.id)

    def _check_ready(self) -> bool:
        if not self.settings.target_url:
            messagebox.showerror("시작 불가", "대상 URL을 먼저 입력하고 'URL 적용'을 누르세요.")
            return False
        return True

    def _start_all(self):
        if not self._check_ready():
            return
        if not any(a.enabled for a in self.settings.accounts):
            messagebox.showerror("시작 불가", "활성화된 계정이 없습니다.")
            return
        self.scheduler.start_all()

    def _stop_all(self):
        self.b_stop.configure(state="disabled")
        self.l_run.configure(text="● 중지하는 중…", fg="#f5b942")
        self.root.update_idletasks()
        self.scheduler.stop_all(wait=None)  # GUI를 막지 않음

    def _set_running(self, running: bool):
        self.b_start.configure(state="disabled" if running else "normal")
        self.b_stop.configure(state="normal")  # 지금 실행 작업도 멈출 수 있게 항상 활성
        self.l_run.configure(text="● 실행 중" if running else "● 정지", fg="#3ecf8e" if running else MUTED)

    # ------------------------------------------------------------------ 표시
    def _row(self, acc: Account):
        st = self.states.get(acc.id, AccountState())
        res = st.last_result
        mode = "즉시" if acc.start_mode == "now" else f"{acc.start_time}부터"
        if acc.start_mode == "now" and acc.end_time:
            mode = f"즉시 ({acc.start_time} 재개)"
        values = (acc.name, acc.login_id, "예" if acc.enabled else "아니오", mode, f"{acc.interval_min:g}",
                  acc.end_time or "없음",
                  st.status, fmt_dt(st.last_start), res.status if res else "-", fmt_dt(st.next_run))
        tag = st.status if st.status in ("실행 중", "실행 대기", "설정 오류") else (res.status if res else st.status)
        return values, (tag,)

    def _refresh_table(self):
        ids = [a.id for a in self.settings.accounts]
        for iid in self.tv.get_children():
            if iid not in ids:
                self.tv.delete(iid)
        for i, acc in enumerate(self.settings.accounts):
            values, tags = self._row(acc)
            if self.tv.exists(acc.id):
                self.tv.item(acc.id, values=values, tags=tags)
                self.tv.move(acc.id, "", i)
            else:
                self.tv.insert("", i, iid=acc.id, values=values, tags=tags)
        self._show_detail()

    def _show_detail(self):
        acc = self._selected()
        for iid in self.tv_det.get_children():
            self.tv_det.delete(iid)
        if not acc:
            self.l_det.configure(text="계정을 선택하세요.")
            return
        st = self.states.get(acc.id)
        res = st.last_result if st else None
        if not res:
            self.l_det.configure(text=f"[{acc.name}] 아직 실행 기록이 없습니다.")
            return
        took = f"{(res.finished - res.started).total_seconds():.0f}초" if res.finished else "-"
        self.l_det.configure(text=f"[{acc.name}] {fmt_dt(res.started)} 시작 · {took} · {res.status} - {res.message}")
        for j in res.jumps:
            self.tv_det.insert("", "end", values=(j.label, j.outcome, j.detail), tags=(j.outcome,))

    def _log_line(self, t: datetime, who: str, level: str, msg: str):
        self.t_log.configure(state="normal")
        self.t_log.insert("end", f"{t:%m-%d %H:%M:%S}  [{who}] {msg}\n", level)
        lines = int(self.t_log.index("end-1c").split(".")[0])
        if lines > 3000:
            self.t_log.delete("1.0", f"{lines - 3000}.0")
        self.t_log.see("end")
        self.t_log.configure(state="disabled")

    def _poll(self):
        dirty = False
        try:
            while True:
                kind, d = self.events.get_nowait()
                if kind == "log":
                    self._log_line(d["time"], d["account"], d["level"], d["msg"])
                elif kind == "state":
                    self.states[d["id"]] = d["state"]
                    dirty = True
                elif kind == "removed":
                    self.states.pop(d["id"], None)
                    dirty = True
                elif kind == "running":
                    self._set_running(d["running"])
        except queue.Empty:
            pass
        if dirty:
            self._refresh_table()
        self.root.after(200, self._poll)

    def _on_close(self):
        if self.scheduler.running or any(s.status in ("실행 중", "실행 대기") for s in self.states.values()):
            if not messagebox.askyesno("종료", "실행 중인 작업을 중지하고 종료할까요?"):
                return
        self.l_run.configure(text="● 종료하는 중…", fg="#f5b942")
        self.root.update_idletasks()
        self.scheduler.shutdown()
        self.root.destroy()


def main():
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir)
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root, data_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
