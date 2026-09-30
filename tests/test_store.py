import aiosqlite
import pytest
from aiogram.fsm.storage.base import StorageKey
from conftest import NOW, SOURCE

from hr_saas import questions
from hr_saas.fsm import SQLiteStorage
from hr_saas.store import Conflict, NotFound, Store, month_start

QS = questions.to_json(questions.parse(SOURCE))


async def tenant(store, slug="casa-luce", email="a@a.example", plan="pro"):
    return await store.create_tenant("Co " + slug, slug, email, "Owner", "hash", NOW, plan)


async def test_tenant_slug_and_email_are_unique_and_validated(store):
    await tenant(store)
    with pytest.raises(Conflict, match="адрес компании"):
        await tenant(store, email="other@a.example")
    with pytest.raises(Conflict, match="e-mail"):
        await tenant(store, slug="other-co")
    for bad in ("Casa Luce", "a", "-start", "end-", "ünicode"):
        with pytest.raises(Conflict, match="slug"):
            await tenant(store, slug=bad, email=f"{len(bad)}@x.example")


async def test_other_company_data_is_invisible(store):
    a, b = await tenant(store), await tenant(store, "studio-b", "b@b.example")
    va = await store.save_vacancy(a, None, "A job", "", QS, True, NOW)
    cand = await store.start_candidate(a, va, 1, "Laura", None, "it", NOW)
    await store.finish_candidate(cand, NOW)
    with pytest.raises(NotFound):
        await store.vacancy(b, va)
    with pytest.raises(NotFound):
        await store.candidate(b, cand)
    with pytest.raises(NotFound):
        await store.answers(b, cand)
    with pytest.raises(NotFound):
        await store.set_status(b, cand, "hired")
    with pytest.raises(NotFound):
        await store.save_vacancy(b, va, "stolen", "", QS, True, NOW)
    with pytest.raises(NotFound):
        await store.add_note(b, cand, 1, "x", NOW)
    assert await store.candidates(b) == [] and await store.export_rows(b) == []
    assert (await store.candidate(a, cand))["status"] == "new"


async def test_one_questionnaire_at_a_time(store):
    a = await tenant(store)
    v = await store.save_vacancy(a, None, "Job", "", QS, True, NOW)
    first = await store.start_candidate(a, v, 7, "Laura", None, "it", NOW)
    second = await store.start_candidate(a, v, 7, "Laura", None, "it", NOW + 1)
    assert (await store.candidate(a, first))["status"] == "abandoned"
    assert (await store.candidate(a, second))["status"] == "in_progress"
    with pytest.raises(aiosqlite.IntegrityError):  # the database itself refuses a second one
        await store.db.execute("INSERT INTO candidates (tenant_id, vacancy_id, tg_id, name, status, created_at) "
                               "VALUES (?, ?, 7, 'x', 'in_progress', 0)", (a, v))


async def test_answers_once_and_score(store):
    a = await tenant(store)
    v = await store.save_vacancy(a, None, "Job", "", QS, True, NOW)
    c = await store.start_candidate(a, v, 7, "Laura", "laura", "it", NOW)
    assert await store.save_answer(c, 0, "Years?", "choice", "1-3", 2, 3, None, NOW)
    assert not await store.save_answer(c, 0, "Years?", "choice", "More than 3", 3, 3, None, NOW)  # double tap
    await store.save_answer(c, 1, "Project?", "open", "python api", 6.5, 10, "ok", NOW)
    assert await store.finish_candidate(c, NOW + 60) == 65.4  # 100 × (2 + 6.5) / (3 + 10)
    assert [r["answer"] for r in await store.answers(a, c)] == ["1-3", "python api"]


async def test_month_counter_and_limits(store):
    a = await tenant(store)
    v = await store.save_vacancy(a, None, "Job", "", QS, True, NOW)
    start = month_start(NOW)
    for i, finished in enumerate((start - 1, start, NOW)):
        c = await store.start_candidate(a, v, 100 + i, "x", None, "en", finished)
        await store.finish_candidate(c, finished)
    assert await store.finished_this_month(a, NOW) == 2
    await store.save_vacancy(a, None, "Off", "", QS, False, NOW)
    assert await store.count_active_vacancies(a) == 1
    assert await store.count_active_vacancies(a, except_id=v) == 0


async def test_candidate_filters_and_like_escaping(store):
    a = await tenant(store)
    v1 = await store.save_vacancy(a, None, "Dev", "", QS, True, NOW)
    v2 = await store.save_vacancy(a, None, "QA", "", QS, True, NOW)
    for tg, v, name, answer, score in ((1, v1, "Laura", "asyncio and 100% tests", 3), (2, v2, "Marco", "django", 0)):
        c = await store.start_candidate(a, v, tg, name, None, "it", NOW)
        await store.save_answer(c, 0, "Q", "choice", answer, score, 3, None, NOW)
        await store.finish_candidate(c, NOW)
    assert [r["name"] for r in await store.candidates(a)] == ["Laura", "Marco"]  # best score first
    assert [r["name"] for r in await store.candidates(a, vacancy_id=v2)] == ["Marco"]
    assert [r["name"] for r in await store.candidates(a, query="asyncio")] == ["Laura"]
    assert [r["name"] for r in await store.candidates(a, query="100%")] == ["Laura"]
    # "%" is a character, not a wildcard: only the answer that really contains it matches
    assert [r["name"] for r in await store.candidates(a, query="%")] == ["Laura"]
    assert [r["name"] for r in await store.candidates(a, status="new")] == ["Laura", "Marco"]


async def test_bot_belongs_to_one_company(store):
    a, b = await tenant(store), await tenant(store, "studio-b", "b@b.example")
    await store.connect_bot(a, 555, "a_bot", "enc", "secret", NOW)
    await store.connect_bot(a, 555, "a_bot", "enc2", "secret2", NOW)  # reconnecting the same bot is fine
    with pytest.raises(Conflict, match="другой компании"):
        await store.connect_bot(b, 555, "a_bot", "enc", "s", NOW)
    assert (await store.bot_by_tg_id(555))["webhook_secret"] == "secret2"


async def test_fsm_survives_a_restart(settings):
    key = StorageKey(bot_id=1, chat_id=2, user_id=2)
    other_bot = StorageKey(bot_id=9, chat_id=2, user_id=2)
    s1 = await Store(settings.database_path).open()
    fsm = SQLiteStorage(s1)
    await fsm.set_state(key, "Survey:answering")
    await fsm.set_data(key, {"candidate_id": 5, "index": 1, "name": "Laura è"})
    await s1.close()
    s2 = await Store(settings.database_path).open()
    fsm2 = SQLiteStorage(s2)
    assert await fsm2.get_state(key) == "Survey:answering"
    assert await fsm2.get_data(key) == {"candidate_id": 5, "index": 1, "name": "Laura è"}
    assert await fsm2.get_state(other_bot) is None and await fsm2.get_data(other_bot) == {}
    await fsm2.set_state(key, None)
    assert await fsm2.get_state(key) is None
    await s2.close()


async def test_dashboard(store):
    a = await tenant(store)
    v = await store.save_vacancy(a, None, "Dev", "", QS, True, NOW)
    c = await store.start_candidate(a, v, 1, "Laura", None, "it", NOW)
    await store.save_answer(c, 0, "Q", "choice", "x", 3, 3, None, NOW)
    await store.finish_candidate(c, NOW)
    await store.start_candidate(a, v, 2, "Marco", None, "it", NOW)
    d = await store.dashboard(a, NOW + 10)
    assert d["by_status"]["new"] == 1 and d["by_status"]["in_progress"] == 1 and d["week"] == 1
    assert [r["name"] for r in d["top"]] == ["Laura"]
