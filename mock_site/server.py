"""로컬 모의 사이트: 로그인 → 업소 정보(점프 4종) → 확인 창 → 대기(쿨다운) 흐름을 재현한다.

실행: python -m mock_site.server [--port 8765] [--cooldown 600] [--slow 40]
대상 URL: http://127.0.0.1:8765/owner

테스트 계정(아이디/비밀번호) — 실제 계정 아님:
  test1/pass1, test2/pass2, test3/pass3  정상
  cool/cool1        네 버튼이 모두 대기(쿨다운) 상태
  nobtn/nobtn1      홍보관 점프 버튼이 없음
  nodialog/nodialog1  매니저 출근부 점프 버튼이 확인 창을 띄우지 않음
  limit/limit1      확인 뒤 '횟수 초과' 알림
  modal/modal1      브라우저 확인 창 대신 HTML 모달 확인 창
  slow/slow1        로그인 후 페이지 응답이 --slow 초 지연(네트워크 지연)
  failjump/failjump1  확인 승인 뒤 점프 요청이 HTTP 500, 화면·횟수 변화 없음
  nosignal/nosignal1  확인 승인 뒤 서버 200(빈 응답)이지만 화면·횟수 변화 없음(성공 신호 없음)
  reload/reload1    점프 성공 뒤 페이지 새로고침(성공 플래그 없음, 새 화면의 대기 상태로 확인)
  quiet/quiet1      점프 성공 응답에 성공 플래그 없음(버튼 대기 전환·횟수 변화로만 확인)
  captcha/captcha1  로그인 뒤 자동입력 방지(CAPTCHA) 화면
조회: GET /api/stats (사용자·점프별 성공 횟수와 기록), 초기화: POST /api/reset
"""

from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

JUMPS = [
    ("lineup", "라인업/PR 점프", "라인업/PR"),
    ("realtime", "실시간 출근부 점프", "실시간 출근부"),
    ("manager", "매니저 출근부 점프", "매니저 출근부"),
    ("promo", "홍보관 점프", "홍보관"),
]
USERS = {
    "test1": "pass1", "test2": "pass2", "test3": "pass3",
    "cool": "cool1", "nobtn": "nobtn1", "nodialog": "nodialog1", "limit": "limit1",
    "modal": "modal1", "slow": "slow1", "captcha": "captcha1",
    "failjump": "failjump1", "nosignal": "nosignal1", "reload": "reload1", "quiet": "quiet1",
}
DAILY_LIMIT = 50


class State:
    def __init__(self, cooldown: float, slow: float):
        self.cooldown = cooldown
        self.slow = slow
        self.lock = threading.Lock()
        self.sessions: dict[str, str] = {}
        self.until: dict[tuple[str, str], float] = {}
        self.log: list[dict] = []
        self.logins: list[dict] = []

    def reset(self):
        with self.lock:
            self.until.clear()
            self.log.clear()
            self.logins.clear()

    def remain(self, user: str, jtype: str) -> float:
        if user == "cool":
            return 600.0
        return max(0.0, self.until.get((user, jtype), 0) - time.time())

    def count(self, user: str, jtype: str) -> int:
        return sum(1 for e in self.log if e["user"] == user and e["type"] == jtype)


LOGIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>로그인 (모의)</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;background:#eee;display:flex;justify-content:center;padding-top:80px}}
.box{{background:#3b3b3b;padding:20px;width:320px}}input{{width:100%;box-sizing:border-box;padding:8px;margin:4px 0}}
.row{{display:flex;gap:6px}}button{{flex:1;padding:9px;border:0;color:#fff}}.login{{background:#8fb3cc}}.back{{background:#c66}}
h1{{font-weight:300}}</style></head><body><div><h1>MOCK SITE</h1><p>모의 로그인 화면 (실제 사이트 아님)</p>
<form class="box" method="post" action="/login"><input type="hidden" name="next" value="{next}">
<input name="mb_id" placeholder="아이디" autocomplete="off"><input type="password" name="mb_password" placeholder="패스워드">
<div class="row"><button class="login" type="submit">로그인</button><button class="back" type="button" onclick="history.back()">돌아가기</button></div>
</form></div>{script}</body></html>"""

OWNER_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>업소 정보 (모의)</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;margin:24px;background:#f5f6f8}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px}}.card button{{width:100%;padding:9px;background:#1769c4;color:#fff;border:0;border-radius:5px}}
.card button:disabled{{background:#7fa6d3}}small{{color:#666;display:block;margin:4px 0}}
.modal{{position:fixed;inset:0;background:#0006;display:none;align-items:center;justify-content:center}}.modal>div{{background:#fff;padding:20px;border-radius:8px}}</style>
</head><body><nav><a href="/">사이트로</a> · <span id="who">업주 · {user}</span> · <a href="/logout">로그아웃</a></nav>
<h2>업소 정보</h2><h3>점프 컨트롤 (4종 분리)</h3><p>각 페이지에서 우리 업소 카드를 상단으로 올립니다. 4개 점프는 별도 카운터/쿨다운으로 동작합니다.</p>
<div class="grid">{cards}</div>
<div class="modal" role="dialog" id="mdl"><div><p id="mdlmsg"></p><button id="mok">확인</button> <button id="mno">취소</button></div></div>
<script>
const USER = {user_js};
function fmt(s){{s=Math.ceil(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}}
function cool(btn, sec){{btn.disabled=true;const end=Date.now()+sec*1000;
  const t=()=>{{const r=(end-Date.now())/1000;if(r<=0){{btn.disabled=false;btn.textContent=btn.dataset.label;return;}}btn.textContent='대기 '+fmt(r);setTimeout(t,500);}};t();}}
function ask(msg){{
  if (USER!=='modal') return Promise.resolve(confirm(msg));
  return new Promise(res=>{{const m=document.getElementById('mdl');document.getElementById('mdlmsg').textContent=msg;m.style.display='flex';
    document.getElementById('mok').onclick=()=>{{m.style.display='none';res(true);}};
    document.getElementById('mno').onclick=()=>{{m.style.display='none';res(false);}};}});
}}
async function jump(btn){{
  const type=btn.dataset.type;
  if (USER==='nodialog' && type==='manager') return;
  if(!(await ask(btn.dataset.page+' 페이지에서 우리 업소 카드를 상단으로 점프할까요?'))) return;
  const r=await fetch('/api/jump',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{type}})}});
  if(!r.ok) return;  // 사이트가 오류를 화면에 알리지 않는 경우(HTTP 500 등)
  const j=await r.json();
  if(j.ok===false){{alert(j.msg);return;}}
  if(USER==='reload'){{location.reload();return;}}
  document.getElementById('cnt-'+type).textContent=j.count;
  cool(btn,j.remain);
}}
document.querySelectorAll('.card button').forEach(b=>{{b.onclick=()=>jump(b);const r=+b.dataset.remain;if(r>0)cool(b,r);}});
</script></body></html>"""

CARD = """<div class="card"><div class="title">{label}</div><small>오늘 <span id="cnt-{t}">{count}</span> / {limit}회</small>
<small>점프 후 재점프 대기 있음</small>
<button data-type="{t}" data-label="{label}" data-page="{page}" data-remain="{remain}">{label}</button></div>"""


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 조용히
            pass

        def _user(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            sid = c["sid"].value if "sid" in c else None
            return sid, state.sessions.get(sid or "")

        def _send(self, code, body="", ctype="text/html; charset=utf-8", headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, to, headers=None):
            self._send(302, "", headers={"Location": to, **(headers or {})})

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            sid, user = self._user()
            if u.path == "/login":
                if user:
                    return self._redirect("/owner")
                nxt = parse_qs(u.query).get("next", ["/"])[0]
                return self._send(200, LOGIN_PAGE.format(next=html.escape(nxt), script=""))
            if u.path == "/logout":
                state.sessions.pop(sid or "", None)
                return self._redirect("/login")
            if u.path == "/api/stats":
                with state.lock:
                    counts: dict[str, dict[str, int]] = {}
                    for e in state.log:
                        counts.setdefault(e["user"], {}).setdefault(e["type"], 0)
                        counts[e["user"]][e["type"]] += 1
                    return self._json({"counts": counts, "log": state.log, "logins": state.logins})
            if u.path == "/owner":
                if not user:
                    return self._redirect("/login?next=/owner")
                if user == "slow":
                    time.sleep(state.slow)
                if user == "captcha":
                    return self._send(200, "<html><body><h2>자동입력 방지</h2><div class='g-recaptcha'>보안 확인</div></body></html>")
                cards = []
                for t, label, page in JUMPS:
                    if user == "nobtn" and t == "promo":
                        continue
                    cards.append(CARD.format(t=t, label=label, page=page, limit=DAILY_LIMIT,
                                             count=state.count(user, t), remain=int(state.remain(user, t))))
                return self._send(200, OWNER_PAGE.format(user=html.escape(user), user_js=json.dumps(user),
                                                         cards="".join(cards)))
            if u.path == "/":
                return self._send(200, "<html><body><h1>모의 사이트 홈</h1><a href='/owner'>업소 정보</a>"
                                       + (f" <span>{html.escape(user)}</span>" if user else " <a href='/login'>로그인</a>")
                                       + "</body></html>")
            self._send(404, "not found")

        def do_POST(self):
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            if u.path == "/login":
                form = {k: v[0] for k, v in parse_qs(body).items()}
                uid, pw = form.get("mb_id", ""), form.get("mb_password", "")
                nxt = form.get("next") or "/"
                if USERS.get(uid) != pw:
                    with state.lock:
                        state.logins.append({"user": uid, "ok": False, "time": datetime.now().isoformat()})
                    return self._send(200, LOGIN_PAGE.format(
                        next=html.escape(nxt), script="<script>alert('아이디 또는 비밀번호가 일치하지 않습니다.')</script>"))
                sid = secrets.token_hex(12)
                with state.lock:
                    state.sessions[sid] = uid
                    state.logins.append({"user": uid, "ok": True, "time": datetime.now().isoformat()})
                return self._redirect(nxt if nxt.startswith("/") else "/",
                                      {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly"})
            if u.path == "/api/reset":
                state.reset()
                return self._json({"ok": True})
            if u.path == "/api/jump":
                sid, user = self._user()
                if not user:
                    return self._json({"ok": False, "msg": "로그인이 필요합니다."}, 401)
                jtype = json.loads(body or "{}").get("type")
                with state.lock:
                    if user == "failjump":
                        return self._json({"error": "internal"}, 500)
                    if user == "nosignal":
                        return self._send(200, "", "text/plain; charset=utf-8")
                    if user == "limit":
                        return self._json({"ok": False, "msg": "오늘 점프 횟수를 초과했습니다."})
                    if state.remain(user, jtype) > 0:
                        return self._json({"ok": False, "msg": "재점프 대기 시간이 남아 있어 점프할 수 없습니다."})
                    state.until[(user, jtype)] = time.time() + state.cooldown
                    state.log.append({"user": user, "type": jtype, "sid": sid[:6],
                                      "time": datetime.now().isoformat(timespec="seconds")})
                    cnt = state.count(user, jtype)
                res = {"count": cnt, "remain": state.cooldown}
                if user not in ("quiet", "reload"):
                    res["ok"] = True  # quiet/reload는 명시적 성공 플래그 없이 화면 변화로만 알린다
                return self._json(res)
            self._send(404, "not found")

    return H


class _Server(ThreadingHTTPServer):
    # Windows에서 SO_REUSEADDR는 이미 사용 중인 포트에도 조용히 바인딩되므로 끈다.
    allow_reuse_address = False


def serve(port: int = 8765, cooldown: float = 600, slow: float = 40, host: str = "127.0.0.1"):
    state = State(cooldown, slow)
    httpd = _Server((host, port), make_handler(state))
    httpd.daemon_threads = True
    httpd.handle_error = lambda request, client_address: None  # 브라우저가 끊은 연결 소음 제거
    return httpd, state


def main():
    ap = argparse.ArgumentParser(description="로컬 모의 점프 사이트")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cooldown", type=float, default=600, help="점프 후 대기 시간(초). 주기 테스트는 0 권장")
    ap.add_argument("--slow", type=float, default=40, help="slow 계정의 응답 지연(초)")
    a = ap.parse_args()
    httpd, _ = serve(a.port, a.cooldown, a.slow)
    print(f"모의 사이트 실행 중: http://127.0.0.1:{httpd.server_port}/owner  (Ctrl+C 종료)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
