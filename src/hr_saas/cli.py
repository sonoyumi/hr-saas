"""hr-saas run | polling | create-tenant | set-plan | tenants | gen-key | check | demo"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import time

from . import plans, questions, security
from .config import ConfigError, Settings, load_settings
from .crypto import CryptoError, Vault, new_key
from .scoring import MockScorer, build_scorer
from .store import Conflict, Store


def _hub(store: Store, settings: Settings):
    from .hub import Hub

    return Hub(store, settings, Vault(settings.master_key),
               build_scorer(settings.ai_mode, settings.anthropic_api_key, settings.claude_model))


async def _run(settings: Settings) -> None:
    import uvicorn

    from .web import create_app

    store = await Store(settings.database_path).open()
    hub = _hub(store, settings)
    server = uvicorn.Server(uvicorn.Config(create_app(store, settings, hub), host=settings.host, port=settings.port,
                                           log_level="warning"))
    print(f"Кабинет: http://{settings.host}:{settings.port} · вебхуки: {settings.public_url}/tg/<id бота>")
    try:
        await server.serve()
    finally:
        await hub.close()
        await store.close()


async def _with_store(settings: Settings, fn):
    store = await Store(settings.database_path).open()
    try:
        return await fn(store)
    finally:
        await store.close()


def _password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Пароль владельца: ")
    if first != getpass.getpass("Ещё раз: "):
        raise SystemExit("Пароли не совпадают.")
    return first


DEMO = [
    ("Casa Luce Bolzano", "casa-luce", "pro", "Giulia Rossi", "giulia@casaluce.example", [
        ("Commesso/a negozio di illuminazione", "Vendita e consulenza ai clienti nel negozio di Bolzano.", """
? Da quanto lavori nella vendita?
- Meno di 1 anno | 0
- 1-3 anni | 2
- Più di 3 anni | 3
? Parli tedesco?
- Sì, fluente | 3
- Base | 1
- No | 0
?? Racconta una vendita difficile che hai chiuso | cliente, ascoltare, soluzione, consiglio
"""),
        ("Magazziniere", "Carico e scarico, inventario, spedizioni.", """
? Hai il patentino del muletto?
- Sì | 3
- No | 0
? Disponibile su turni?
- Sì | 2
- Solo mattina | 1
?? Come organizzi un inventario? | scanner, excel, ordine, controllo
"""),
    ], [
        (910101, "Laura Bianchi", "laurab", 0, ["1-3 anni", "Sì, fluente"],
         "Un cliente voleva restituire una lampada: ho ascoltato il problema e proposto una soluzione con un "
         "consiglio su un modello più adatto. Alla fine ha comprato due lampade.", "shortlist"),
        (910102, "Marco Neri", "mneri", 0, ["Più di 3 anni", "Base"],
         "Ho convinto un cliente indeciso spiegando i vantaggi del LED.", "new"),
        (910103, "Olha Kovalenko", "olha_k", 0, ["Meno di 1 anno", "Sì, fluente"],
         "Ascoltare il cliente è la cosa più importante, poi trovare una soluzione.", "new"),
        (910104, "Stefan Huber", None, 1, ["Sì", "Sì"],
         "Uso lo scanner e un foglio Excel, controllo ogni scaffale in ordine.", "hired"),
        (910105, "Paolo Ferri", "pferri", 1, ["No", "Solo mattina"], "Conto tutto.", "rejected"),
    ]),
    ("Studio Rossi Commercialisti", "studio-rossi", "free", "Andrea Rossi", "andrea@studiorossi.example", [
        ("Impiegato/a contabile", "Prima nota, fatture elettroniche, F24.", """
? Conosci la fattura elettronica (FatturaPA)?
- Sì, la uso ogni giorno | 3
- Un po' | 1
- No | 0
?? Come controlli una fattura prima di registrarla? | partita iva, importi, iva, scadenza
"""),
    ], [
        (910201, "Elena Conti", "econti", 0, ["Sì, la uso ogni giorno"],
         "Controllo la partita IVA, gli importi e l'IVA, poi la scadenza del pagamento.", "new"),
    ]),
]


async def _demo(store: Store, settings: Settings) -> str:
    """Two fictional companies with vacancies and candidates — for showing the cabinet (not a real database!)."""
    now = time.time()
    scorer = MockScorer()
    for company, slug, plan_code, owner, email, vacancies, people in DEMO:
        if await store.tenant_by_slug(slug):
            continue
        tid = await store.create_tenant(company, slug, email, owner, security.hash_password("demo-password-123"),
                                        now - 20 * 86400, plan_code)
        vids = [await store.save_vacancy(tid, None, title, desc, questions.to_json(questions.parse(src)), True,
                                         now - 15 * 86400) for title, desc, src in vacancies]
        for n, (tg, name, user, vi, picks, open_text, status) in enumerate(people):
            qs = questions.parse(vacancies[vi][2])
            at = now - (5 - n) * 86400
            cid = await store.start_candidate(tid, vids[vi], tg, name, user, "it", at)
            choice = [q for q in qs if q.kind == "choice"]
            for i, q in enumerate(qs):
                if q.kind == "choice":
                    o = next(o for o in q.options if o.text == picks[choice.index(q)])
                    await store.save_answer(cid, i, q.text, "choice", o.text, o.points, q.max_points, None, at)
                else:
                    s = await scorer.score(q.text, open_text, q.keywords)
                    await store.save_answer(cid, i, q.text, "open", open_text, s.points, 10, s.comment, at)
            await store.finish_candidate(cid, at + 300)
            if status != "new":
                await store.set_status(tid, cid, status)
    return "Демо-компании: giulia@casaluce.example и andrea@studiorossi.example, пароль demo-password-123."


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hr-saas", description="HR-скрининг для многих компаний")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="кабинет компаний + приём вебхуков Telegram")
    p = sub.add_parser("polling", help="бот одной компании без HTTPS (для проверки на своём компьютере)")
    p.add_argument("slug")
    p = sub.add_parser("create-tenant", help="создать компанию и владельца")
    p.add_argument("name")
    p.add_argument("--slug", required=True)
    p.add_argument("--owner", required=True, help="имя владельца")
    p.add_argument("--email", required=True)
    p.add_argument("--plan", default="free", choices=list(plans.PLANS))
    p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("set-plan", help="сменить тариф компании")
    p.add_argument("slug")
    p.add_argument("plan", choices=list(plans.PLANS))
    sub.add_parser("tenants", help="компании и их использование")
    sub.add_parser("gen-key", help="новый MASTER_KEY для шифрования токенов")
    sub.add_parser("check", help="проверить настройки")
    sub.add_parser("demo", help="две вымышленные компании для показа (не на рабочей базе!)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "gen-key":
        print(new_key())
        return
    try:
        settings = load_settings()
        Vault(settings.master_key)
    except (ConfigError, CryptoError) as exc:
        raise SystemExit(str(exc)) from None

    if args.cmd == "run":
        asyncio.run(_run(settings))
    elif args.cmd == "polling":
        async def poll(store: Store):
            hub = _hub(store, settings)
            try:
                await hub.polling(args.slug)
            except Conflict as exc:
                raise SystemExit(str(exc)) from None
            finally:
                await hub.close()
        asyncio.run(_with_store(settings, poll))
    elif args.cmd == "create-tenant":
        password = _password(args.password_stdin)
        if len(password) < 10:
            raise SystemExit("Пароль короче 10 символов.")

        async def create(store: Store):
            try:
                return await store.create_tenant(args.name, args.slug, args.email, args.owner,
                                                 security.hash_password(password), time.time(), args.plan)
            except Conflict as exc:
                raise SystemExit(str(exc)) from None
        tid = asyncio.run(_with_store(settings, create))
        print(f"Компания «{args.name}» (id {tid}, тариф {args.plan}) создана. Вход: {args.email}")
    elif args.cmd == "set-plan":
        ok = asyncio.run(_with_store(settings, lambda s: s.set_plan(args.slug, args.plan)))
        print(f"Тариф {args.slug}: {args.plan}." if ok else f"Нет компании {args.slug}.")
    elif args.cmd == "tenants":
        rows = asyncio.run(_with_store(settings, lambda s: s.tenants(time.time())))
        print(f"{'адрес':<16} {'тариф':<9} {'польз.':>6} {'вакансий':>9} {'анкет/мес':>10}  бот")
        for r in rows:
            p = plans.plan(r["plan"])
            month = f"{r['month']}/{p.candidates_month if p.candidates_month is not None else '∞'}"
            print(f"{r['slug']:<16} {r['plan']:<9} {r['users']:>6} {r['vacancies']:>9} {month:>10}  "
                  f"{'@' + r['bot'] if r['bot'] else '—'}")
    elif args.cmd == "check":
        rows = asyncio.run(_with_store(settings, lambda s: s.tenants(time.time())))
        print("Настройки в порядке.")
        print(f"База: {settings.database_path} · компаний: {len(rows)}")
        print(f"Адрес для вебхуков: {settings.public_url}"
              + ("" if settings.public_url.startswith("https://") else "  ⚠️ не HTTPS — Telegram не пришлёт вебхуки"))
        print(f"Оценка открытых ответов: {settings.ai_mode}"
              + (f" ({settings.claude_model})" if settings.ai_mode == "claude" else ""))
    elif args.cmd == "demo":
        print(asyncio.run(_with_store(settings, lambda s: _demo(s, settings))))


if __name__ == "__main__":
    main()
