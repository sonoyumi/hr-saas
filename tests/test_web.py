from io import BytesIO

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe, SetWebhook
from conftest import NOW, SOURCE, login
from openpyxl import load_workbook

from hr_saas import questions, security

GOOD = {"company": "Hotel Alpina", "slug": "hotel-alpina", "name": "Anna", "email": "anna@alpina.example",
        "password": "correct horse battery"}


async def owner_id(store, email):
    return (await store.user_by_email(email))["id"]


async def finished(store, tenant_id, vacancy_id, name="Laura", answer="Python API", tg=1):
    c = await store.start_candidate(tenant_id, vacancy_id, tg, name, "laura", "it", NOW)
    await store.save_answer(c, 0, "Project?", "open", answer, 7, 10, "ok", NOW)
    await store.finish_candidate(c, NOW)
    return c


async def test_signup_creates_company_and_logs_in(web, store):
    r = await web.post("/signup", data=GOOD)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    assert "HttpOnly" in r.headers["set-cookie"]
    page = await web.get("/dashboard")
    assert page.status_code == 200 and "Hotel Alpina" in page.text and "Запуск за 3 шага" in page.text
    assert (await store.tenant_by_slug("hotel-alpina"))["plan"] == "free"


async def test_signup_errors_all_at_once(web):
    r = await web.post("/signup", data={**GOOD, "company": "", "email": "nope", "password": "short"})
    assert r.status_code == 400
    for text in ("название компании", "Нужен e-mail", "не короче 10"):
        assert text in r.text
    await web.post("/signup", data=GOOD)
    web.cookies.clear()
    r = await web.post("/signup", data={**GOOD, "email": "second@alpina.example"})
    assert "адрес компании уже занят" in r.text


async def test_login_limit_and_open_redirect(web, company_a):
    for _ in range(5):
        assert (await web.post("/login", data={"email": "giulia@a.example", "password": "nope"})).status_code == 401
    assert (await web.post("/login", data={"email": "giulia@a.example",
                                           "password": "correct horse battery"})).status_code == 429


async def test_login_ok_ignores_foreign_next(web, company_a):
    r = await web.post("/login", data={"email": "GIULIA@a.example", "password": "correct horse battery",
                                       "next": "//evil.example"})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"


async def test_pages_need_login(web):
    for path in ("/dashboard", "/candidates/1", "/export.xlsx", "/bot"):
        r = await web.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


async def test_company_cannot_see_another_company(web, store, company_a, company_b):
    cand = await finished(store, company_a["id"], company_a["vacancy"])
    login(web, await owner_id(store, "andrea@b.example"))
    assert (await web.get(f"/candidates/{cand}")).status_code == 404
    assert (await web.get(f"/vacancies/{company_a['vacancy']}")).status_code == 404
    assert "Laura" not in (await web.get("/candidates")).text
    wb = load_workbook(BytesIO((await web.get("/export.xlsx")).content))
    assert wb["Кандидаты"].max_row == 1  # header only


async def test_candidate_page_escapes_answers_and_changes_status(web, store, company_a):
    cand = await finished(store, company_a["id"], company_a["vacancy"], answer="<script>alert(1)</script>")
    csrf = login(web, await owner_id(store, "giulia@a.example"))
    page = await web.get(f"/candidates/{cand}")
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text
    assert (await web.post(f"/candidates/{cand}/status", data={"status": "hired", "csrf": "x"})).status_code == 403
    await web.post(f"/candidates/{cand}/status", data={"status": "shortlist", "csrf": csrf})
    await web.post(f"/candidates/{cand}/note", data={"text": "Call on Friday", "csrf": csrf})
    assert (await store.candidate(company_a["id"], cand))["status"] == "shortlist"
    assert "Call on Friday" in (await web.get(f"/candidates/{cand}")).text
    assert (await web.post(f"/candidates/{cand}/status", data={"status": "in_progress", "csrf": csrf})).status_code \
        == 403


async def test_vacancy_editor_shows_every_problem(web, store, company_a):
    csrf = login(web, await owner_id(store, "giulia@a.example"))
    r = await web.post("/vacancies/save", data={"title": "", "source": "- orphan\n? One\n- a | 99",
                                                "active": "1", "csrf": csrf})
    for text in ("Укажите название", "строка 1", "баллы от 0 до 10"):
        assert text in r.text
    assert len(await store.vacancies(company_a["id"])) == 1


async def test_plan_limit_for_active_vacancies(web, store, company_b):
    csrf = login(web, await owner_id(store, "andrea@b.example"))  # Free: 1 active vacancy
    r = await web.post("/vacancies/save", data={"title": "Second", "source": SOURCE, "active": "1", "csrf": csrf})
    assert "активных вакансий не больше 1" in r.text
    ok = await web.post("/vacancies/save", data={"title": "Draft", "source": SOURCE, "csrf": csrf})  # inactive
    assert ok.status_code == 303
    vid = int(ok.headers["location"].split("/")[2].split("?")[0])
    edit = await web.get(f"/vacancies/{vid}")
    assert "? Years with Python?" in edit.text  # the questionnaire goes back into the editor as text


async def test_export_neutralises_formulas(web, store, company_a):
    await finished(store, company_a["id"], company_a["vacancy"], name="=HYPERLINK(\"http://x\")",
                   answer="+SUM(A1:A9)")
    login(web, await owner_id(store, "giulia@a.example"))
    r = await web.get("/export.xlsx")
    assert r.headers["content-disposition"] == 'attachment; filename="candidates-casa-luce.xlsx"'
    wb = load_workbook(BytesIO(r.content))
    assert wb["Кандидаты"]["B2"].value == "'=HYPERLINK(\"http://x\")" and wb["Кандидаты"]["B2"].data_type == "s"
    assert wb["Ответы"]["D2"].value == "'+SUM(A1:A9)"


async def test_bot_connect_errors_and_owner_only(web, store, clock, tg):
    tid = await store.create_tenant("New Co", "new-co", "o@new.example", "Owner", "h", NOW)
    csrf = login(web, await owner_id(store, "o@new.example"))
    r = await web.post("/bot/connect", data={"token": "hello", "csrf": csrf})
    assert "токен выглядит неверно" in (await web.get(r.headers["location"])).text
    tg.fail_with = [TelegramUnauthorizedError(GetMe(), "Unauthorized")]
    r = await web.post("/bot/connect", data={"token": "700009:AAinvalid_token_not_real_00000000", "csrf": csrf})
    assert "Telegram не принял токен" in (await web.get(r.headers["location"])).text
    r = await web.post("/bot/connect", data={"token": "700009:AAvalid_token_not_real_000000000", "csrf": csrf})
    assert "@hr_700009_bot подключён" in (await web.get(r.headers["location"])).text
    assert tg.of(SetWebhook)[-1].url == "https://hr.example/tg/700009"
    recruiter = await store.add_user(tid, "r@new.example", "Rec", "h", NOW)
    csrf_r = login(web, recruiter)
    assert (await web.post("/bot/disconnect", data={"csrf": csrf_r})).status_code == 403


async def test_team_temp_password_shown_once(web, store, company_b):
    csrf = login(web, await owner_id(store, "andrea@b.example"))
    r = await web.post("/team/add", data={"name": "Rec", "email": "rec@b.example", "csrf": csrf})
    assert r.status_code == 303 and "На тарифе не больше 1" in (await web.get(r.headers["location"])).text
    await store.set_plan("studio-rossi", "pro")
    r = await web.post("/team/add", data={"name": "Rec", "email": "rec@b.example", "csrf": csrf})
    assert r.status_code == 200 and "Временный пароль" in r.text  # shown in the response, no redirect / URL
    temp = r.text.split("Временный пароль: <code>")[1].split("</code>")[0]
    user = await store.user_by_email("rec@b.example")
    assert security.verify_password(temp, user["password_hash"]) and temp not in user["password_hash"]
    assert "Временный пароль" not in (await web.get("/team")).text


async def test_pages_render(web, store, company_a):
    await finished(store, company_a["id"], company_a["vacancy"])
    login(web, await owner_id(store, "giulia@a.example"))
    for path in ("/dashboard", "/vacancies", "/vacancies/new", f"/vacancies/{company_a['vacancy']}",
                 "/candidates", "/candidates?status=new&q=Python", "/bot", "/team", "/plan"):
        r = await web.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-security-policy"].startswith("default-src 'self'")


async def test_vacancy_list_counts_questions(web, store, company_a):
    login(web, await owner_id(store, "giulia@a.example"))
    page = (await web.get("/vacancies")).text
    assert "Python developer" in page and f'<td class="num">{len(questions.parse(SOURCE))}</td>' in page
