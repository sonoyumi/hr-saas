"""SQLite storage for many companies (tenants) in one database.

Tenant isolation rule: every function that reads or changes company data takes ``tenant_id`` and puts it
into the WHERE clause. A candidate id from another company behaves exactly like a missing one
(NotFound), so ids cannot be guessed across companies.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

STATUSES = ("new", "shortlist", "rejected", "hired")  # visible in the cabinet
ALL_STATUSES = ("in_progress", "abandoned", *STATUSES)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'free',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'recruiter')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bots (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id),
    tg_bot_id INTEGER NOT NULL UNIQUE,
    username TEXT NOT NULL,
    token_enc TEXT NOT NULL,
    webhook_secret TEXT NOT NULL,
    connected_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    questions TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS vacancies_tenant ON vacancies(tenant_id, active);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id),
    tg_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    username TEXT,
    lang TEXT NOT NULL DEFAULT 'en',
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'abandoned', 'new', 'shortlist', 'rejected', 'hired')),
    score REAL,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_questionnaire ON candidates(tenant_id, tg_id) WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS candidates_list ON candidates(tenant_id, status, score);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    q_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    kind TEXT NOT NULL,
    answer TEXT NOT NULL,
    points REAL NOT NULL,
    max_points REAL NOT NULL,
    comment TEXT,
    created_at REAL NOT NULL,
    UNIQUE (candidate_id, q_index)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fsm (
    key TEXT PRIMARY KEY,
    state TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
"""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


def month_start(now: float) -> float:
    d = datetime.fromtimestamp(now, UTC)
    return datetime(d.year, d.month, 1, tzinfo=UTC).timestamp()


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> Store:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store.open() first"
        return self._db

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                await self.db.rollback()
                raise
            else:
                await self.db.commit()

    async def _one(self, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, args) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ tenants and users
    async def create_tenant(self, name: str, slug: str, owner_email: str, owner_name: str, password_hash: str,
                            now: float, plan: str = "free") -> int:
        if not SLUG.match(slug):
            raise Conflict("slug: 3–40 латинских строчных букв, цифр и дефисов")
        async with self._tx() as db:
            try:
                cur = await db.execute("INSERT INTO tenants (name, slug, plan, created_at) VALUES (?, ?, ?, ?)",
                                       (name, slug, plan, now))
                tenant_id = cur.lastrowid
                await db.execute("INSERT INTO users (tenant_id, email, name, password_hash, role, created_at) "
                                 "VALUES (?, ?, ?, ?, 'owner', ?)", (tenant_id, owner_email, owner_name,
                                                                     password_hash, now))
            except aiosqlite.IntegrityError as exc:
                what = "адрес компании" if "slug" in str(exc) else "e-mail"
                raise Conflict(f"{what} уже занят") from None
            return tenant_id

    async def tenant(self, tenant_id: int) -> aiosqlite.Row:
        row = await self._one("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
        if row is None:
            raise NotFound(tenant_id)
        return row

    async def tenant_by_slug(self, slug: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM tenants WHERE slug = ?", (slug,))

    async def set_plan(self, slug: str, plan: str) -> bool:
        async with self._tx() as db:
            cur = await db.execute("UPDATE tenants SET plan = ? WHERE slug = ?", (plan, slug))
            return cur.rowcount == 1

    async def tenants(self, now: float) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT t.*, (SELECT COUNT(*) FROM users u WHERE u.tenant_id = t.id AND u.active = 1) AS users, "
            "(SELECT COUNT(*) FROM vacancies v WHERE v.tenant_id = t.id AND v.active = 1) AS vacancies, "
            "(SELECT COUNT(*) FROM candidates c WHERE c.tenant_id = t.id AND c.finished_at >= ?) AS month, "
            "(SELECT username FROM bots b WHERE b.tenant_id = t.id) AS bot "
            "FROM tenants t ORDER BY t.id", (month_start(now),))

    async def add_user(self, tenant_id: int, email: str, name: str, password_hash: str, now: float,
                       role: str = "recruiter") -> int:
        async with self._tx() as db:
            try:
                cur = await db.execute("INSERT INTO users (tenant_id, email, name, password_hash, role, created_at) "
                                       "VALUES (?, ?, ?, ?, ?, ?)", (tenant_id, email, name, password_hash, role, now))
            except aiosqlite.IntegrityError:
                raise Conflict("e-mail уже занят") from None
            return cur.lastrowid

    async def user_by_email(self, email: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM users WHERE email = ? AND active = 1", (email,))

    async def user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT u.*, t.name AS company, t.plan, t.slug FROM users u "
                               "JOIN tenants t ON t.id = u.tenant_id WHERE u.id = ? AND u.active = 1", (user_id,))

    async def users(self, tenant_id: int) -> list[aiosqlite.Row]:
        return await self._all("SELECT id, email, name, role FROM users WHERE tenant_id = ? AND active = 1 "
                               "ORDER BY role DESC, name", (tenant_id,))

    async def deactivate_user(self, tenant_id: int, user_id: int) -> bool:
        async with self._tx() as db:
            cur = await db.execute("UPDATE users SET active = 0 WHERE id = ? AND tenant_id = ? AND role != 'owner'",
                                   (user_id, tenant_id))
            return cur.rowcount == 1

    # ------------------------------------------------------------------ bots
    async def connect_bot(self, tenant_id: int, tg_bot_id: int, username: str, token_enc: str, secret: str,
                          now: float) -> None:
        async with self._tx() as db:
            async with db.execute("SELECT tenant_id FROM bots WHERE tg_bot_id = ?", (tg_bot_id,)) as cur:
                other = await cur.fetchone()
            if other is not None and other["tenant_id"] != tenant_id:
                raise Conflict("этот бот уже подключён к другой компании")
            await db.execute(
                "INSERT INTO bots (tenant_id, tg_bot_id, username, token_enc, webhook_secret, connected_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET tg_bot_id = excluded.tg_bot_id, "
                "username = excluded.username, token_enc = excluded.token_enc, "
                "webhook_secret = excluded.webhook_secret, connected_at = excluded.connected_at",
                (tenant_id, tg_bot_id, username, token_enc, secret, now))

    async def bot_of(self, tenant_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM bots WHERE tenant_id = ?", (tenant_id,))

    async def bot_by_tg_id(self, tg_bot_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT b.*, t.name AS company, t.plan FROM bots b JOIN tenants t "
                               "ON t.id = b.tenant_id WHERE b.tg_bot_id = ?", (tg_bot_id,))

    async def all_bots(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM bots ORDER BY tenant_id")

    async def disconnect_bot(self, tenant_id: int) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM bots WHERE tenant_id = ?", (tenant_id,))

    # ------------------------------------------------------------------ vacancies
    async def vacancies(self, tenant_id: int, only_active: bool = False) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT v.*, (SELECT COUNT(*) FROM candidates c WHERE c.vacancy_id = v.id AND c.status IN "
            "('new', 'shortlist', 'rejected', 'hired')) AS candidates, (SELECT ROUND(AVG(score)) FROM candidates c "
            "WHERE c.vacancy_id = v.id AND c.score IS NOT NULL) AS avg_score "
            "FROM vacancies v WHERE v.tenant_id = ?" + (" AND v.active = 1" if only_active else "") +
            " ORDER BY v.active DESC, v.updated_at DESC", (tenant_id,))

    async def vacancy(self, tenant_id: int, vacancy_id: int) -> aiosqlite.Row:
        row = await self._one("SELECT * FROM vacancies WHERE id = ? AND tenant_id = ?", (vacancy_id, tenant_id))
        if row is None:
            raise NotFound(vacancy_id)
        return row

    async def save_vacancy(self, tenant_id: int, vacancy_id: int | None, title: str, description: str,
                           questions: list[dict], active: bool, now: float) -> int:
        data = json.dumps(questions, ensure_ascii=False)
        async with self._tx() as db:
            if vacancy_id is None:
                cur = await db.execute(
                    "INSERT INTO vacancies (tenant_id, title, description, questions, active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (tenant_id, title, description, data, int(active), now, now))
                return cur.lastrowid
            cur = await db.execute("UPDATE vacancies SET title = ?, description = ?, questions = ?, active = ?, "
                                   "updated_at = ? WHERE id = ? AND tenant_id = ?",
                                   (title, description, data, int(active), now, vacancy_id, tenant_id))
            if cur.rowcount != 1:
                raise NotFound(vacancy_id)
            return vacancy_id

    async def count_active_vacancies(self, tenant_id: int, except_id: int | None = None) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM vacancies WHERE tenant_id = ? AND active = 1 AND id != ?",
                              (tenant_id, except_id or 0))
        return row["n"]

    # ------------------------------------------------------------------ candidates (bot side)
    async def start_candidate(self, tenant_id: int, vacancy_id: int, tg_id: int, name: str, username: str | None,
                              lang: str, now: float) -> int:
        """A new questionnaire; an unfinished previous one becomes 'abandoned' (in one transaction)."""
        async with self._tx() as db:
            await db.execute("UPDATE candidates SET status = 'abandoned' WHERE tenant_id = ? AND tg_id = ? "
                             "AND status = 'in_progress'", (tenant_id, tg_id))
            cur = await db.execute(
                "INSERT INTO candidates (tenant_id, vacancy_id, tg_id, name, username, lang, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?)", (tenant_id, vacancy_id, tg_id, name, username, lang,
                                                                 now))
            return cur.lastrowid

    async def save_answer(self, candidate_id: int, q_index: int, question: str, kind: str, answer: str,
                          points: float, max_points: float, comment: str | None, now: float) -> bool:
        """False if this question was already answered (a double tap): the first answer stays."""
        async with self._tx() as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO answers (candidate_id, q_index, question, kind, answer, points, max_points, "
                "comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, q_index, question, kind, answer, points, max_points, comment, now))
            return cur.rowcount == 1

    async def finish_candidate(self, candidate_id: int, now: float) -> float | None:
        """Score = 100 × points / max points over all answers; the questionnaire becomes 'new'."""
        async with self._tx() as db:
            async with db.execute("SELECT SUM(points) AS p, SUM(max_points) AS m FROM answers WHERE candidate_id = ?",
                                  (candidate_id,)) as cur:
                row = await cur.fetchone()
            score = round(100 * row["p"] / row["m"], 1) if row["m"] else None
            await db.execute("UPDATE candidates SET status = 'new', score = ?, finished_at = ? WHERE id = ? "
                             "AND status = 'in_progress'", (score, now, candidate_id))
            return score

    async def finished_this_month(self, tenant_id: int, now: float) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM candidates WHERE tenant_id = ? AND finished_at >= ?",
                              (tenant_id, month_start(now)))
        return row["n"]

    # ------------------------------------------------------------------ candidates (cabinet)
    async def candidates(self, tenant_id: int, *, vacancy_id: int | None = None, status: str | None = None,
                         query: str = "", limit: int = 200) -> list[aiosqlite.Row]:
        where, args = ["c.tenant_id = ?", "c.status IN ('new', 'shortlist', 'rejected', 'hired')"], [tenant_id]
        if vacancy_id:
            where.append("c.vacancy_id = ?")
            args.append(vacancy_id)
        if status in STATUSES:
            where.append("c.status = ?")
            args.append(status)
        if query.strip():
            like = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(c.name LIKE ? ESCAPE '\\' OR c.username LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM "
                         "answers a WHERE a.candidate_id = c.id AND a.answer LIKE ? ESCAPE '\\'))")
            args += [like, like, like]
        return await self._all(
            "SELECT c.*, v.title AS vacancy FROM candidates c JOIN vacancies v ON v.id = c.vacancy_id "
            f"WHERE {' AND '.join(where)} ORDER BY c.score IS NULL, c.score DESC, c.finished_at DESC LIMIT ?",
            (*args, limit))

    async def candidate(self, tenant_id: int, candidate_id: int) -> aiosqlite.Row:
        row = await self._one("SELECT c.*, v.title AS vacancy FROM candidates c JOIN vacancies v ON v.id = "
                              "c.vacancy_id WHERE c.id = ? AND c.tenant_id = ?", (candidate_id, tenant_id))
        if row is None:
            raise NotFound(candidate_id)
        return row

    async def answers(self, tenant_id: int, candidate_id: int) -> list[aiosqlite.Row]:
        await self.candidate(tenant_id, candidate_id)
        return await self._all("SELECT * FROM answers WHERE candidate_id = ? ORDER BY q_index", (candidate_id,))

    async def notes(self, tenant_id: int, candidate_id: int) -> list[aiosqlite.Row]:
        await self.candidate(tenant_id, candidate_id)
        return await self._all("SELECT n.*, u.name AS author FROM notes n JOIN users u ON u.id = n.user_id "
                               "WHERE n.candidate_id = ? ORDER BY n.id", (candidate_id,))

    async def set_status(self, tenant_id: int, candidate_id: int, status: str) -> None:
        if status not in STATUSES:
            raise Conflict(status)
        async with self._tx() as db:
            cur = await db.execute("UPDATE candidates SET status = ? WHERE id = ? AND tenant_id = ? "
                                   "AND status IN ('new', 'shortlist', 'rejected', 'hired')",
                                   (status, candidate_id, tenant_id))
            if cur.rowcount != 1:
                raise NotFound(candidate_id)

    async def add_note(self, tenant_id: int, candidate_id: int, user_id: int, text: str, now: float) -> None:
        await self.candidate(tenant_id, candidate_id)
        async with self._tx() as db:
            await db.execute("INSERT INTO notes (candidate_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                             (candidate_id, user_id, text, now))

    async def dashboard(self, tenant_id: int, now: float) -> dict:
        by = {r["status"]: r["n"] for r in await self._all(
            "SELECT status, COUNT(*) AS n FROM candidates WHERE tenant_id = ? GROUP BY status", (tenant_id,))}
        week = await self._one("SELECT COUNT(*) AS n FROM candidates WHERE tenant_id = ? AND finished_at >= ?",
                               (tenant_id, now - 7 * 86400))
        top = await self._all(
            "SELECT c.id, c.name, c.score, v.title AS vacancy FROM candidates c JOIN vacancies v ON v.id = "
            "c.vacancy_id WHERE c.tenant_id = ? AND c.status = 'new' AND c.score IS NOT NULL "
            "ORDER BY c.score DESC LIMIT 5", (tenant_id,))
        return {"by_status": {s: by.get(s, 0) for s in ALL_STATUSES}, "week": week["n"],
                "month": await self.finished_this_month(tenant_id, now), "top": top}

    # ------------------------------------------------------------------ export
    async def export_rows(self, tenant_id: int, vacancy_id: int | None = None) -> list[aiosqlite.Row]:
        extra, args = ("AND c.vacancy_id = ?", (tenant_id, vacancy_id)) if vacancy_id else ("", (tenant_id,))
        return await self._all(
            "SELECT c.id, c.name, c.username, v.title AS vacancy, c.status, c.score, c.finished_at, a.q_index, "
            "a.question, a.answer, a.points, a.max_points, a.comment FROM candidates c JOIN vacancies v ON v.id = "
            "c.vacancy_id LEFT JOIN answers a ON a.candidate_id = c.id WHERE c.tenant_id = ? AND c.status IN "
            f"('new', 'shortlist', 'rejected', 'hired') {extra} ORDER BY c.score DESC, c.id, a.q_index", args)
