"""Many companies' bots in one process.

Telegram sends every bot's updates to ``POST /tg/<bot id>`` with the secret we chose when setting the
webhook (header ``X-Telegram-Bot-Api-Secret-Token``). The hub finds the company by the bot id, checks the
secret in constant time, and feeds the update to ONE shared Dispatcher together with the company data.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Update

from . import plans
from .bot import make_router
from .config import Settings
from .crypto import Vault
from .fsm import SQLiteStorage
from .scoring import KeywordScorer, Scorer
from .store import Conflict, Store

log = logging.getLogger(__name__)
TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
ALLOWED_UPDATES = ["message", "callback_query"]


class Hub:
    def __init__(self, store: Store, settings: Settings, vault: Vault, ai_scorer: Scorer,
                 session: BaseSession | None = None, clock: Callable[[], float] = time.time) -> None:
        self.store, self.settings, self.vault, self.ai_scorer = store, settings, vault, ai_scorer
        self.session, self.clock = session, clock
        self.dp = Dispatcher(storage=SQLiteStorage(store), events_isolation=SimpleEventIsolation())
        self.dp.include_router(make_router(store, clock))
        self._bots: dict[int, Bot] = {}

    def _bot(self, token: str) -> Bot:
        return Bot(token, session=self.session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    def bot_for(self, row) -> Bot:
        """One Bot object per company bot, created on first use (token decrypted only in memory)."""
        bot = self._bots.get(row["tg_bot_id"])
        if bot is None:
            bot = self._bots[row["tg_bot_id"]] = self._bot(self.vault.decrypt(row["token_enc"]))
        return bot

    def context(self, row) -> dict:
        plan = plans.plan(row["plan"])
        return {"tenant": {"tenant_id": row["tenant_id"], "company": row["company"], "plan": row["plan"]},
                "scorer": self.ai_scorer if plan.ai else KeywordScorer()}

    async def handle(self, tg_bot_id: int, secret: str | None, payload: dict) -> int:
        """HTTP status for Telegram: 200 even if a handler fails (otherwise Telegram re-sends forever)."""
        row = await self.store.bot_by_tg_id(tg_bot_id)
        if row is None:
            return 404
        if not secret or not hmac.compare_digest(secret, row["webhook_secret"]):
            return 403
        bot = self.bot_for(row)
        try:
            update = Update.model_validate(payload, context={"bot": bot})
            await self.dp.feed_update(bot, update, **self.context(row))
        except Exception:  # noqa: BLE001 — log it, answer 200, keep serving other companies
            log.exception("update for bot %s failed", tg_bot_id)
        return 200

    async def connect(self, tenant_id: int, token: str) -> str:
        """Check the token with getMe, store it encrypted, point the webhook here. Returns @username."""
        token = token.strip()
        if not TOKEN.match(token):
            raise Conflict("токен выглядит неверно: он вида 123456789:AA… (от @BotFather)")
        bot = self._bot(token)
        try:
            try:
                me = await bot.get_me()
            except TelegramUnauthorizedError:
                raise Conflict("Telegram не принял токен: проверьте его в @BotFather") from None
            except TelegramAPIError as exc:
                raise Conflict(f"Telegram: {exc.message}") from None
            secret = secrets.token_urlsafe(32)
            await self.store.connect_bot(tenant_id, me.id, me.username or str(me.id), self.vault.encrypt(token),
                                         secret, self.clock())
            self._bots.pop(me.id, None)
            if self.settings.public_url.startswith("https://"):
                try:
                    await bot.set_webhook(f"{self.settings.public_url}/tg/{me.id}", secret_token=secret,
                                          allowed_updates=ALLOWED_UPDATES, drop_pending_updates=True)
                except TelegramAPIError as exc:
                    raise Conflict(f"бот подключён, но вебхук не установлен: {exc.message}") from None
            return me.username or str(me.id)
        finally:
            if self.session is None:  # a temporary Bot with its own HTTP session
                await bot.session.close()

    async def disconnect(self, tenant_id: int) -> None:
        row = await self.store.bot_of(tenant_id)
        if row is None:
            return
        try:
            await self.bot_for(row).delete_webhook()
        except TelegramAPIError as exc:
            log.warning("delete_webhook failed: %s", exc.message)
        self._bots.pop(row["tg_bot_id"], None)
        await self.store.disconnect_bot(tenant_id)

    async def polling(self, slug: str) -> None:
        """Development without HTTPS: one company's bot with long polling."""
        tenant = await self.store.tenant_by_slug(slug)
        if tenant is None:
            raise Conflict(f"нет компании {slug}")
        row = await self.store.bot_of(tenant["id"])
        if row is None:
            raise Conflict("у компании не подключён бот")
        bot = self.bot_for(await self.store.bot_by_tg_id(row["tg_bot_id"]))
        await bot.delete_webhook()
        await self.dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES,
                                    **self.context(await self.store.bot_by_tg_id(row["tg_bot_id"])))

    async def close(self) -> None:
        for bot in self._bots.values():
            await bot.session.close()
