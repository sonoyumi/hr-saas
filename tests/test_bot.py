"""The candidate's side, end to end: JSON update → POST /tg/<bot id> → hub → Dispatcher → store."""

import httpx
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SetWebhook
from conftest import NOW, TOKEN_A, TOKEN_B, Candidate

from hr_saas.bot import Ans, Vac
from hr_saas.crypto import Vault
from hr_saas.hub import Hub
from hr_saas.scoring import MockScorer
from hr_saas.store import Store
from hr_saas.web import create_app


async def test_connect_set_the_webhook_with_a_secret(company_a, tg, store):
    [hook] = tg.of(SetWebhook)
    row = await store.bot_by_tg_id(700001)
    assert hook.url == "https://hr.example/tg/700001" and hook.secret_token == row["webhook_secret"]
    assert hook.allowed_updates == ["message", "callback_query"] and hook.drop_pending_updates
    assert "AAtoken" not in row["token_enc"]  # stored encrypted


async def test_full_questionnaire(company_a, web, store, tg):
    c = Candidate(web, store, company_a["bot"])
    assert (await c.say("/start")).status_code == 200
    greeting = tg.sent(TOKEN_A)[-1]
    assert "Casa Luce" in greeting.text and greeting.text.startswith("Buongiorno")  # Italian: language_code it
    assert [b.text for row in greeting.reply_markup.inline_keyboard for b in row] == ["Python developer"]
    await c.press(Vac(id=company_a["vacancy"]).pack())
    q1 = tg.sent(TOKEN_A)[-1]
    assert "Domanda 1/2" in q1.text and len(q1.reply_markup.inline_keyboard) == 3
    cand = (await store.db.execute_fetchall("SELECT id FROM candidates"))[0]["id"]
    await c.press(Ans(c=cand, q=0, o=1).pack())  # "1-3" → 2 of 3 points
    assert "Domanda 2/2" in tg.sent(TOKEN_A)[-1].text
    await c.say("I built a REST API in Python with tests for a hotel")
    assert tg.sent(TOKEN_A)[-1].text.startswith("Grazie, Laura!")
    row = await store.candidate(company_a["id"], cand)
    assert row["status"] == "new" and row["score"] == round(100 * (2 + 10) / 13, 1)
    answers = await store.answers(company_a["id"], cand)
    assert answers[1]["comment"].startswith("[DEMO]")  # Pro plan uses the configured (mock) AI scorer


async def test_stale_and_double_taps(company_a, web, store, tg):
    c = Candidate(web, store, company_a["bot"])
    await c.say("/start")
    await c.press(Vac(id=company_a["vacancy"]).pack())
    cand = (await store.db.execute_fetchall("SELECT id FROM candidates"))[0]["id"]
    await c.press(Ans(c=cand, q=0, o=2).pack())
    await c.press(Ans(c=cand, q=0, o=0).pack())  # an old button under question 1
    await c.press(Ans(c=cand + 99, q=1, o=0).pack())  # a button from another questionnaire
    answers = await store.answers(company_a["id"], cand)
    assert [a["answer"] for a in answers] == ["More than 3"]
    alerts = [m.text for m in tg.of(AnswerCallbackQuery) if m.text]
    assert alerts == ["Questo pulsante non è più attivo."] * 2
    assert len(tg.of(EditMessageText)) == 1  # only the real answer marked the question ✅


async def test_text_when_buttons_are_expected(company_a, web, store, tg):
    c = Candidate(web, store, company_a["bot"], lang="en", first_name="Tom")
    await c.say("/start")
    await c.press(Vac(id=company_a["vacancy"]).pack())
    await c.say("five years")
    assert tg.sent(TOKEN_A)[-1].text == "For this question, please choose one of the buttons above."


async def test_free_plan_limit_pauses_applications(company_b, web, store, tg, clock):
    for i in range(30):  # Free: 30 finished questionnaires per month
        cid = await store.start_candidate(company_b["id"], company_b["vacancy"], 9000 + i, "x", None, "it", clock())
        await store.finish_candidate(cid, clock())
    c = Candidate(web, store, company_b["bot"], lang="uk")
    await c.say("/start")
    await c.press(Vac(id=company_b["vacancy"]).pack())
    assert tg.sent(TOKEN_B)[-1].text.startswith("Прийом анкет тимчасово призупинено")
    assert await store.db.execute_fetchall("SELECT 1 FROM candidates WHERE tg_id = 5001") == []


async def test_other_company_vacancy_cannot_be_opened(company_a, company_b, web, store, tg):
    c = Candidate(web, store, company_b["bot"])
    await c.say("/start")
    await c.press(Vac(id=company_a["vacancy"]).pack())  # forged: A's vacancy through B's bot
    assert [m.text for m in tg.of(AnswerCallbackQuery)][-1] == "Questa posizione non è più disponibile."
    assert await store.candidates(company_a["id"]) == [] and await store.candidates(company_b["id"]) == []


async def test_no_active_vacancies(company_a, web, store, tg):
    v = await store.vacancy(company_a["id"], company_a["vacancy"])
    await store.save_vacancy(company_a["id"], v["id"], v["title"], "", [], False, NOW)
    await Candidate(web, store, company_a["bot"], lang="ru").say("/start")
    assert tg.sent(TOKEN_A)[-1].text == "Сейчас открытых вакансий нет. Загляните позже!"


async def test_webhook_security(company_a, web, store, tg):
    c = Candidate(web, store, company_a["bot"])
    assert (await c.say("/start", secret="guess")).status_code == 403
    assert (await c.say("/start", secret="")).status_code == 403
    assert (await web.post("/tg/123", json={"update_id": 1})).status_code == 404
    bad = await web.post(f"/tg/{company_a['bot']}", content=b"{not json",
                         headers={"X-Telegram-Bot-Api-Secret-Token": "x"})
    assert bad.status_code == 400
    assert tg.sent(TOKEN_A) == []


async def test_handler_error_still_answers_200(company_a, web, store, tg):
    tg.fail_with = [RuntimeError("telegram is down")]
    assert (await Candidate(web, store, company_a["bot"]).say("/start")).status_code == 200


async def test_questionnaire_continues_after_restart(company_a, web, store, settings, tg, clock):
    c = Candidate(web, store, company_a["bot"])
    await c.say("/start")
    await c.press(Vac(id=company_a["vacancy"]).pack())
    cand = (await store.db.execute_fetchall("SELECT id FROM candidates"))[0]["id"]
    await c.press(Ans(c=cand, q=0, o=0).pack())
    # "restart": a brand-new Store, Hub and app on the same database file
    store2 = await Store(settings.database_path).open()
    hub2 = Hub(store2, settings, Vault(settings.master_key), MockScorer(), session=tg, clock=clock)
    app2 = create_app(store2, settings, hub2, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://test") as web2:
        c.web, c.store = web2, store2
        await c.say("Python API and tests")
    row = await store2.candidate(company_a["id"], cand)
    assert row["status"] == "new" and row["score"] == round(100 * (0 + 10) / 13, 1)
    await store2.close()
