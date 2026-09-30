"""Fixtures: database, clock, a fake Telegram (records Bot API calls, answers getMe/setWebhook), the hub,
the cabinet client and a candidate who talks to a company bot THROUGH THE WEBHOOK URL."""

from __future__ import annotations

import datetime as dt
import itertools

import httpx
import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteWebhook,
    EditMessageText,
    GetMe,
    SendMessage,
    SetWebhook,
    TelegramMethod,
)
from aiogram.types import Chat, Message, User

from hr_saas import questions, security
from hr_saas.config import Settings
from hr_saas.crypto import Vault, new_key
from hr_saas.hub import Hub
from hr_saas.scoring import MockScorer
from hr_saas.store import Store
from hr_saas.web import COOKIE, create_app

NOW = 1_790_000_000.0  # 2026-09-21
SECRET = "s" * 40
TOKEN_A = "700001:AAtoken_for_company_a_not_real_0000"
TOKEN_B = "700002:AAtoken_for_company_b_not_real_0000"
SOURCE = """? Years with Python?
- Less than 1 | 0
- 1-3 | 2
- More than 3 | 3
?? Your hardest project | python, api, tests
"""


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, TelegramMethod]] = []
        self.fail_with: list[Exception] = []
        self.ids = itertools.count(100)

    async def close(self) -> None:
        return None

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 — aiogram BaseSession signature
        self.calls.append((bot.token, method))
        if self.fail_with:
            raise self.fail_with.pop(0)
        if isinstance(method, GetMe):
            bot_id = int(bot.token.split(":")[0])
            return User(id=bot_id, is_bot=True, first_name="HR", username=f"hr_{bot_id}_bot")
        if isinstance(method, SetWebhook | DeleteWebhook | AnswerCallbackQuery):
            return True
        if isinstance(method, SendMessage | EditMessageText):
            return Message(message_id=next(self.ids), date=dt.datetime.fromtimestamp(NOW, dt.UTC),
                           chat=Chat(id=method.chat_id or 1, type="private"), text=method.text)
        raise AssertionError(f"unexpected {type(method).__name__}")

    def sent(self, token: str | None = None) -> list[SendMessage]:
        return [m for t, m in self.calls if isinstance(m, SendMessage) and (token is None or t == token)]

    def of(self, kind) -> list:
        return [m for _, m in self.calls if isinstance(m, kind)]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(secret_key=SECRET, master_key=new_key(), public_url="https://hr.example",
                    database_path=tmp_path / "hr.db", ai_mode="mock")


@pytest.fixture
async def store(settings):
    s = await Store(settings.database_path).open()
    yield s
    await s.close()


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def hub(store, settings, tg, clock) -> Hub:
    return Hub(store, settings, Vault(settings.master_key), MockScorer(), session=tg, clock=clock)


@pytest.fixture
async def company_a(store, hub, clock):
    """Pro company with a connected bot and one active vacancy."""
    tid = await store.create_tenant("Casa Luce", "casa-luce", "giulia@a.example", "Giulia",
                                    security.hash_password("correct horse battery"), clock(), "pro")
    await hub.connect(tid, TOKEN_A)
    vid = await store.save_vacancy(tid, None, "Python developer", "Backend in Bolzano",
                                   questions.to_json(questions.parse(SOURCE)), True, clock())
    return {"id": tid, "vacancy": vid, "bot": 700001}


@pytest.fixture
async def company_b(store, hub, clock):
    tid = await store.create_tenant("Studio Rossi", "studio-rossi", "andrea@b.example", "Andrea",
                                    security.hash_password("another long password"), clock(), "free")
    await hub.connect(tid, TOKEN_B)
    vid = await store.save_vacancy(tid, None, "Accountant", "", questions.to_json(questions.parse(SOURCE)), True,
                                   clock())
    return {"id": tid, "vacancy": vid, "bot": 700002}


@pytest.fixture
async def web(store, settings, hub, clock):
    app = create_app(store, settings, hub, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def login(client: httpx.AsyncClient, user_id: int, now: float = NOW) -> str:
    cookie = security.make_session(SECRET, user_id, now)
    client.cookies.set(COOKIE, cookie)
    return security.csrf_token(SECRET, cookie)


class Candidate:
    """Sends Telegram-shaped JSON updates to POST /tg/<bot id> with the bot's secret header."""

    def __init__(self, web: httpx.AsyncClient, store: Store, bot_id: int, user_id: int = 5001, lang: str = "it",
                 first_name: str = "Laura") -> None:
        self.web, self.store, self.bot_id = web, store, bot_id
        self.user = {"id": user_id, "is_bot": False, "first_name": first_name, "last_name": "Bianchi",
                     "username": first_name.lower(), "language_code": lang}
        self.n = 0

    async def _secret(self) -> str:
        return (await self.store.bot_by_tg_id(self.bot_id))["webhook_secret"]

    async def post(self, update: dict, secret: str | None = None) -> httpx.Response:
        self.n += 1
        update = {"update_id": self.n, **update}
        return await self.web.post(f"/tg/{self.bot_id}", json=update, headers={
            "X-Telegram-Bot-Api-Secret-Token": secret if secret is not None else await self._secret()})

    async def say(self, text: str, **kw) -> httpx.Response:
        msg = {"message_id": 1000 + self.n, "date": int(NOW), "chat": {"id": self.user["id"], "type": "private"},
               "from": self.user, "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return await self.post({"message": msg}, **kw)

    async def press(self, data: str, text: str = "…") -> httpx.Response:
        msg = {"message_id": 2000 + self.n, "date": int(NOW), "chat": {"id": self.user["id"], "type": "private"},
               "text": text}
        return await self.post({"callback_query": {"id": str(self.n), "from": self.user, "chat_instance": "x",
                                                   "message": msg, "data": data}})
