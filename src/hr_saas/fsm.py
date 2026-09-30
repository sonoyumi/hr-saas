"""aiogram FSM storage in SQLite: a half-finished questionnaire survives a restart or a deploy.

MemoryStorage would lose every candidate's progress on restart; Redis would be one more service to run.
The key includes the bot id, so the same Telegram user answering two companies' bots keeps two
independent questionnaires.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .store import Store


def key_str(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.destiny}"


class SQLiteStorage(BaseStorage):
    def __init__(self, store: Store) -> None:
        self.store = store

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self.store._tx() as db:
            await db.execute("INSERT INTO fsm (key, state) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                             "state = excluded.state", (key_str(key), value))

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self.store._one("SELECT state FROM fsm WHERE key = ?", (key_str(key),))
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        async with self.store._tx() as db:
            await db.execute("INSERT INTO fsm (key, data) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                             "data = excluded.data", (key_str(key), json.dumps(dict(data), ensure_ascii=False)))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self.store._one("SELECT data FROM fsm WHERE key = ?", (key_str(key),))
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:  # the Store is closed by its owner
        return None
