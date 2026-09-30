"""The candidate's side: ONE aiogram router serves the bots of ALL companies.

The webhook (hub.py) passes the company as ``tenant`` and its scorer as ``scorer`` into every handler, so
nothing here is tied to one bot. Buttons carry the candidate id and the question number: an old
button (a previous question or another questionnaire) is detected and ignored.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import plans, texts
from .questions import Question, from_json
from .scoring import Scorer
from .store import NotFound, Store

MAX_ANSWER = 2000


class Survey(StatesGroup):
    answering = State()


class Vac(CallbackData, prefix="v"):
    id: int


class Ans(CallbackData, prefix="a"):
    c: int  # candidate id
    q: int  # question index
    o: int  # option index


def vacancies_kb(rows) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for v in rows:
        kb.button(text=v["title"], callback_data=Vac(id=v["id"]))
    kb.adjust(1)
    return kb.as_markup()


def options_kb(candidate_id: int, index: int, q: Question) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(q.options):
        kb.button(text=o.text, callback_data=Ans(c=candidate_id, q=index, o=i))
    kb.adjust(1)
    return kb.as_markup()


def make_router(store: Store, clock: Callable[[], float] = time.time) -> Router:
    router = Router(name="candidates")
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    def lang(event: Message | CallbackQuery) -> str:
        return texts.lang_of(event.from_user.language_code if event.from_user else None)

    async def ask(message: Message, state: FSMContext, lng: str) -> None:
        data = await state.get_data()
        questions = from_json(data["questions"])
        i = data["index"]
        if i >= len(questions):
            await finish(message, state, lng)
            return
        q = questions[i]
        hint = texts.t(lng, "hint_choice" if q.kind == "choice" else "hint_open")
        await message.answer(texts.t(lng, "question", n=i + 1, total=len(questions), text=texts.esc(q.text))
                             + f"\n\n<i>{hint}</i>",
                             reply_markup=options_kb(data["candidate_id"], i, q) if q.kind == "choice" else None)

    async def finish(message: Message, state: FSMContext, lng: str) -> None:
        data = await state.get_data()
        await store.finish_candidate(data["candidate_id"], clock())
        await state.clear()
        await message.answer(texts.t(lng, "done", name=texts.esc(data.get("first_name", "")),
                                     company=texts.esc(data["company"])))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, tenant) -> None:
        await state.clear()
        rows = await store.vacancies(tenant["tenant_id"], only_active=True)
        lng = lang(message)
        if not rows:
            await message.answer(texts.t(lng, "no_vacancies"))
            return
        await message.answer(texts.t(lng, "greeting", company=texts.esc(tenant["company"])),
                             reply_markup=vacancies_kb(rows))

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.t(lang(message), "cancelled"))

    @router.callback_query(Vac.filter())
    async def choose(callback: CallbackQuery, callback_data: Vac, state: FSMContext, tenant) -> None:
        lng = lang(callback)
        try:
            vacancy = await store.vacancy(tenant["tenant_id"], callback_data.id)  # only this company's vacancies
        except NotFound:
            vacancy = None
        if vacancy is None or not vacancy["active"]:
            await callback.answer(texts.t(lng, "closed"), show_alert=True)
            return
        limit = plans.plan(tenant["plan"]).candidates_month
        if not plans.within(limit, await store.finished_this_month(tenant["tenant_id"], clock())):
            await callback.answer()
            await callback.message.answer(texts.t(lng, "limit"))
            return
        user = callback.from_user
        candidate_id = await store.start_candidate(tenant["tenant_id"], vacancy["id"], user.id, user.full_name,
                                                   user.username, lng, clock())
        await state.set_state(Survey.answering)
        await state.set_data({"candidate_id": candidate_id, "index": 0, "questions": json.loads(vacancy["questions"]),
                              "company": tenant["company"], "first_name": user.first_name})
        await callback.answer()
        title = f"<b>{texts.esc(vacancy['title'])}</b>"
        if vacancy["description"]:
            title += f"\n{texts.esc(vacancy['description'])}"
        await callback.message.answer(title)
        await ask(callback.message, state, lng)

    @router.callback_query(Ans.filter(), Survey.answering)
    async def option(callback: CallbackQuery, callback_data: Ans, state: FSMContext) -> None:
        lng = lang(callback)
        data = await state.get_data()
        questions = from_json(data["questions"])
        if callback_data.c != data["candidate_id"] or callback_data.q != data["index"] \
                or not 0 <= callback_data.o < len(questions[data["index"]].options):
            await callback.answer(texts.t(lng, "stale"))
            return
        q = questions[data["index"]]
        chosen = q.options[callback_data.o]
        saved = await store.save_answer(data["candidate_id"], data["index"], q.text, "choice", chosen.text,
                                        chosen.points, q.max_points, None, clock())
        await callback.answer()
        if not saved:  # a double tap: the first answer already moved the questionnaire on
            return
        await state.update_data(index=data["index"] + 1)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(f"{callback.message.html_text}\n\n✅ <i>{texts.esc(chosen.text)}</i>",
                                             reply_markup=None)
            await ask(callback.message, state, lng)

    @router.callback_query(Ans.filter())
    async def option_outside(callback: CallbackQuery) -> None:
        await callback.answer(texts.t(lang(callback), "stale"))

    @router.message(Survey.answering, F.text)
    async def open_answer(message: Message, state: FSMContext, scorer: Scorer) -> None:
        lng = lang(message)
        data = await state.get_data()
        q = from_json(data["questions"])[data["index"]]
        if q.kind != "open":
            await message.answer(texts.t(lng, "use_buttons"))
            return
        text = message.text.strip()
        if len(text) > MAX_ANSWER:
            await message.answer(texts.t(lng, "too_long", limit=MAX_ANSWER))
            return
        s = await scorer.score(q.text, text, q.keywords)
        if await store.save_answer(data["candidate_id"], data["index"], q.text, "open", text, s.points, 10,
                                   s.comment, clock()):
            await state.update_data(index=data["index"] + 1)
        await ask(message, state, lng)

    @router.message()
    async def other(message: Message, state: FSMContext, tenant) -> None:
        if await state.get_state() is None:
            await start(message, state, tenant)

    return router
