"""Company cabinet (FastAPI + Jinja2) and the Telegram webhook endpoint.

Every cabinet page needs a signed session; every form carries a CSRF token; every query goes through
the Store with the user's tenant_id, so one company never sees another's data.
"""

from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import export, plans, questions, security, texts
from .config import Settings
from .hub import Hub
from .store import STATUSES, Conflict, NotFound, Store

HERE = Path(__file__).parent
COOKIE = "hr_session"
EXAMPLE = """? Сколько лет вы работаете с Python?
- Меньше года | 0
- 1–3 года | 2
- Больше 3 лет | 3

? Готовы работать в офисе в Больцано?
- Да | 2
- Только гибрид | 1
- Только удалённо | 0

?? Расскажите о самом сложном проекте | python, api, тесты, база данных
"""


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class Limiter:
    """At most 5 failed logins per IP in 5 minutes."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock, self.fails = clock, defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self.fails[ip]
        while q and q[0] < self.clock() - 300:
            q.popleft()
        return len(q) >= 5

    def fail(self, ip: str) -> None:
        self.fails[ip].append(self.clock())


def create_app(store: Store, settings: Settings, hub: Hub, clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="HR SaaS", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(STATUS=texts.STATUS, ROLE=texts.ROLE, PLANS=plans.PLANS, platform=settings.platform_name)
    tpl.env.filters.update(t=texts.local_time, fromjson=json.loads)
    limiter = Limiter(clock)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("Действие отклонено: обновите страницу и повторите.", status_code=403)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return HTMLResponse("Не найдено.", status_code=404)

    async def user(request: Request):
        uid = security.read_session(settings.secret_key, request.cookies.get(COOKIE), clock())
        row = await store.user(uid) if uid is not None else None
        if row is None:
            raise LoginRequired
        return row

    def csrf(request: Request, token: str) -> None:
        if not security.check_csrf(settings.secret_key, request.cookies.get(COOKIE), token):
            raise Forbidden

    def owner_only(u) -> None:
        if u["role"] != "owner":
            raise Forbidden

    def page(request: Request, name: str, u, **ctx) -> HTMLResponse:
        cookie = request.cookies.get(COOKIE, "")
        return tpl.TemplateResponse(request, name, {"me": u, "csrf": security.csrf_token(settings.secret_key, cookie),
                                                    "plan": plans.plan(u["plan"]) if u else None, **ctx})

    def signed_in(target: str, user_id: int) -> RedirectResponse:
        r = RedirectResponse(target, status_code=303)
        r.set_cookie(COOKIE, security.make_session(settings.secret_key, user_id, clock()), max_age=security.SESSION_TTL,
                     httponly=True, samesite="lax", secure=settings.cookie_secure)
        return r

    # ------------------------------------------------------------------ Telegram webhook
    @app.post("/tg/{tg_bot_id}")
    async def webhook(request: Request, tg_bot_id: int):
        try:
            payload = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"ok": False}, status_code=400)
        status = await hub.handle(tg_bot_id, request.headers.get("X-Telegram-Bot-Api-Secret-Token"), payload)
        return JSONResponse({"ok": status == 200}, status_code=status)

    @app.get("/health")
    async def health():
        return {"ok": True}

    # ------------------------------------------------------------------ sign up, log in
    @app.get("/signup", response_class=HTMLResponse)
    async def signup_form(request: Request):
        return tpl.TemplateResponse(request, "signup.html", {"me": None, "err": [], "form": {}})

    @app.post("/signup")
    async def signup(request: Request, company: str = Form(""), slug: str = Form(""), name: str = Form(""),
                     email: str = Form(""), password: str = Form("")):
        form = {"company": company.strip(), "slug": slug.strip().lower(), "name": name.strip(), "email": email.strip()}
        err = [m for ok, m in ((form["company"], "Укажите название компании"), (form["name"], "Укажите ваше имя"),
                               ("@" in form["email"], "Нужен e-mail"),
                               (len(password) >= 10, "Пароль — не короче 10 символов")) if not ok]
        if not err:
            try:
                tenant_id = await store.create_tenant(form["company"], form["slug"], form["email"], form["name"],
                                                      security.hash_password(password), clock())
            except Conflict as exc:
                err.append(str(exc))
        if err:
            return tpl.TemplateResponse(request, "signup.html", {"me": None, "err": err, "form": form}, status_code=400)
        owner = await store.user_by_email(form["email"])
        assert owner["tenant_id"] == tenant_id
        return signed_in("/dashboard", owner["id"])

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/dashboard"):
        return tpl.TemplateResponse(request, "login.html", {"me": None, "next": next, "err": ""})

    @app.post("/login")
    async def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            return tpl.TemplateResponse(request, "login.html", {"me": None, "next": next,
                                        "err": "Слишком много попыток. Подождите 5 минут."}, status_code=429)
        u = await store.user_by_email(email.strip())
        if u is None or not security.verify_password(password, u["password_hash"]):
            limiter.fail(ip)
            return tpl.TemplateResponse(request, "login.html", {"me": None, "next": next,
                                        "err": "Неверный e-mail или пароль."}, status_code=401)
        target = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
        return signed_in(target, u["id"])

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form("", alias="csrf")):
        csrf(request, csrf_token)
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    # ------------------------------------------------------------------ dashboard
    @app.get("/")
    async def home():
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        u = await user(request)
        tid = u["tenant_id"]
        p = plans.plan(u["plan"])
        d = await store.dashboard(tid, clock())
        usage = {"vacancies": (await store.count_active_vacancies(tid), p.vacancies),
                 "month": (d["month"], p.candidates_month), "team": (len(await store.users(tid)), p.recruiters)}
        return page(request, "dashboard.html", u, d=d, usage=usage, bot=await store.bot_of(tid),
                    has_vacancy=bool(await store.vacancies(tid)), pct=plans.usage_percent, active="dashboard")

    # ------------------------------------------------------------------ vacancies
    @app.get("/vacancies", response_class=HTMLResponse)
    async def vacancy_list(request: Request):
        u = await user(request)
        return page(request, "vacancies.html", u, rows=await store.vacancies(u["tenant_id"]), active="vacancies")

    @app.get("/vacancies/new", response_class=HTMLResponse)
    async def vacancy_new(request: Request):
        u = await user(request)
        return page(request, "vacancy.html", u, v={"id": "", "title": "", "description": "", "active": 1},
                    source=EXAMPLE, err=[], active="vacancies")

    @app.get("/vacancies/{vacancy_id}", response_class=HTMLResponse)
    async def vacancy_edit(request: Request, vacancy_id: int):
        u = await user(request)
        v = await store.vacancy(u["tenant_id"], vacancy_id)
        return page(request, "vacancy.html", u, v=v, source=questions.render(questions.from_json(
            json.loads(v["questions"]))), err=[], active="vacancies")

    @app.post("/vacancies/save")
    async def vacancy_save(request: Request, id: str = Form(""), title: str = Form(""), description: str = Form(""),
                           source: str = Form(""), active: str = Form(""), csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        tid = u["tenant_id"]
        vacancy_id = int(id) if id.isdigit() else None
        err: list[str] = []
        if not title.strip():
            err.append("Укажите название вакансии")
        try:
            parsed = questions.parse(source)
        except questions.QuestionsError as exc:
            parsed, err = [], err + exc.problems
        limit = plans.plan(u["plan"]).vacancies
        if active and not plans.within(limit, await store.count_active_vacancies(tid, vacancy_id)):
            err.append(f"На тарифе «{plans.plan(u['plan']).title}» активных вакансий не больше {limit}. "
                       "Выключите другую вакансию или смените тариф.")
        if err:
            v = {"id": id, "title": title, "description": description, "active": 1 if active else 0}
            return page(request, "vacancy.html", u, v=v, source=source, err=err, active="vacancies")
        vid = await store.save_vacancy(tid, vacancy_id, title.strip()[:120], description.strip()[:1000],
                                       questions.to_json(parsed), bool(active), clock())
        return RedirectResponse(f"/vacancies/{vid}?saved=1", status_code=303)

    # ------------------------------------------------------------------ candidates
    @app.get("/candidates", response_class=HTMLResponse)
    async def candidate_list(request: Request, vacancy: int = 0, status: str = "", q: str = ""):
        u = await user(request)
        rows = await store.candidates(u["tenant_id"], vacancy_id=vacancy or None, status=status or None, query=q)
        return page(request, "candidates.html", u, rows=rows, vacancies=await store.vacancies(u["tenant_id"]),
                    f={"vacancy": vacancy, "status": status, "q": q}, statuses=STATUSES, active="candidates")

    @app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
    async def candidate_card(request: Request, candidate_id: int):
        u = await user(request)
        tid = u["tenant_id"]
        return page(request, "candidate.html", u, c=await store.candidate(tid, candidate_id),
                    answers=await store.answers(tid, candidate_id), notes=await store.notes(tid, candidate_id),
                    statuses=STATUSES, active="candidates")

    @app.post("/candidates/{candidate_id}/status")
    async def candidate_status(request: Request, candidate_id: int, status: str = Form(""),
                               csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        try:
            await store.set_status(u["tenant_id"], candidate_id, status)
        except Conflict:
            raise Forbidden from None
        return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)

    @app.post("/candidates/{candidate_id}/note")
    async def candidate_note(request: Request, candidate_id: int, text: str = Form(""),
                             csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        if text.strip():
            await store.add_note(u["tenant_id"], candidate_id, u["id"], text.strip()[:2000], clock())
        return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)

    @app.get("/export.xlsx")
    async def export_xlsx(request: Request, vacancy: int = 0):
        u = await user(request)
        data = export.build(await store.export_rows(u["tenant_id"], vacancy or None))
        name = f"candidates-{u['slug']}.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ bot, team, plan
    @app.get("/bot", response_class=HTMLResponse)
    async def bot_page(request: Request, err: str = "", ok: str = ""):
        u = await user(request)
        return page(request, "bot.html", u, bot=await store.bot_of(u["tenant_id"]), err=err, ok=ok,
                    https=settings.public_url.startswith("https://"), active="bot")

    @app.post("/bot/connect")
    async def bot_connect(request: Request, token: str = Form(""), csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        owner_only(u)
        try:
            username = await hub.connect(u["tenant_id"], token)
        except Conflict as exc:
            return RedirectResponse("/bot?err=" + quote(str(exc)), status_code=303)
        return RedirectResponse("/bot?ok=" + quote(f"Бот @{username} подключён"), status_code=303)

    @app.post("/bot/disconnect")
    async def bot_disconnect(request: Request, csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        owner_only(u)
        await hub.disconnect(u["tenant_id"])
        return RedirectResponse("/bot", status_code=303)

    @app.get("/team", response_class=HTMLResponse)
    async def team(request: Request, err: str = ""):
        u = await user(request)
        return page(request, "team.html", u, users=await store.users(u["tenant_id"]), created="", password="",
                    err=err, active="team")

    @app.post("/team/add")
    async def team_add(request: Request, name: str = Form(""), email: str = Form(""),
                       csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        owner_only(u)
        limit = plans.plan(u["plan"]).recruiters
        if not plans.within(limit, len(await store.users(u["tenant_id"]))):
            return RedirectResponse("/team?err=" + quote(f"На тарифе не больше {limit} пользователей"), 303)
        if not name.strip() or "@" not in email:
            return RedirectResponse("/team?err=" + quote("Нужны имя и e-mail"), 303)
        temp = secrets.token_urlsafe(9)
        try:
            await store.add_user(u["tenant_id"], email.strip(), name.strip(), security.hash_password(temp), clock())
        except Conflict as exc:
            return RedirectResponse("/team?err=" + quote(str(exc)), 303)
        # The temporary password is shown ONCE, in this response only: not in a URL (history, server logs)
        # and not in the database (only its scrypt hash is stored).
        return page(request, "team.html", u, users=await store.users(u["tenant_id"]), created=email.strip(),
                    password=temp, err="", active="team")

    @app.post("/team/{user_id}/remove")
    async def team_remove(request: Request, user_id: int, csrf_token: str = Form("", alias="csrf")):
        u = await user(request)
        csrf(request, csrf_token)
        owner_only(u)
        await store.deactivate_user(u["tenant_id"], user_id)
        return RedirectResponse("/team", status_code=303)

    @app.get("/plan", response_class=HTMLResponse)
    async def plan_page(request: Request):
        u = await user(request)
        return page(request, "plan.html", u, active="plan")

    return app
