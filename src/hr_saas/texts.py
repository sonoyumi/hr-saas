"""What the bot says to candidates (4 languages, fallback English) and labels for the cabinet."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
LANGS = ("it", "en", "uk", "ru")

BOT = {
    "greeting": {
        "it": "Buongiorno! Sono l'assistente di selezione di «{company}».\n\nScelga la posizione che le interessa: "
              "le farò alcune domande, bastano pochi minuti.",
        "en": "Hello! I'm the recruiting assistant of «{company}».\n\nPick the position you are interested in: "
              "I'll ask a few questions, it takes a few minutes.",
        "uk": "Вітаємо! Я помічник з підбору персоналу «{company}».\n\nОберіть вакансію: я поставлю кілька "
              "запитань, це займе кілька хвилин.",
        "ru": "Здравствуйте! Я помощник по подбору персонала «{company}».\n\nВыберите вакансию: я задам несколько "
              "вопросов, это займёт пару минут.",
    },
    "no_vacancies": {
        "it": "Al momento non ci sono posizioni aperte. Torni a trovarci presto!",
        "en": "There are no open positions right now. Please check back soon!",
        "uk": "Зараз відкритих вакансій немає. Загляньте пізніше!",
        "ru": "Сейчас открытых вакансий нет. Загляните позже!",
    },
    "closed": {
        "it": "Questa posizione non è più disponibile.",
        "en": "This position is no longer available.",
        "uk": "Ця вакансія вже недоступна.",
        "ru": "Эта вакансия уже недоступна.",
    },
    "limit": {
        "it": "La raccolta delle candidature è temporaneamente sospesa. Riprovi tra qualche giorno.",
        "en": "Applications are temporarily paused. Please try again in a few days.",
        "uk": "Прийом анкет тимчасово призупинено. Спробуйте за кілька днів.",
        "ru": "Приём анкет временно приостановлен. Попробуйте через несколько дней.",
    },
    "question": {
        "it": "<b>Domanda {n}/{total}</b>\n{text}",
        "en": "<b>Question {n}/{total}</b>\n{text}",
        "uk": "<b>Питання {n}/{total}</b>\n{text}",
        "ru": "<b>Вопрос {n}/{total}</b>\n{text}",
    },
    "hint_open": {"it": "Risponda con un messaggio.", "en": "Please answer with a message.",
                  "uk": "Дайте відповідь повідомленням.", "ru": "Ответьте сообщением."},
    "hint_choice": {"it": "Scelga una risposta.", "en": "Choose an answer.",
                    "uk": "Оберіть відповідь.", "ru": "Выберите ответ."},
    "use_buttons": {"it": "Per questa domanda scelga uno dei pulsanti qui sopra.",
                    "en": "For this question, please choose one of the buttons above.",
                    "uk": "Для цього питання оберіть одну з кнопок вище.",
                    "ru": "Для этого вопроса выберите одну из кнопок выше."},
    "too_long": {"it": "Risposta troppo lunga: al massimo {limit} caratteri.",
                 "en": "The answer is too long: {limit} characters at most.",
                 "uk": "Відповідь задовга: не більше {limit} символів.",
                 "ru": "Ответ слишком длинный: не больше {limit} символов."},
    "stale": {"it": "Questo pulsante non è più attivo.", "en": "This button is no longer active.",
              "uk": "Ця кнопка вже неактивна.", "ru": "Эта кнопка уже неактивна."},
    "done": {
        "it": "Grazie, {name}! Le sue risposte sono state inviate a «{company}». La contatteremo presto.",
        "en": "Thank you, {name}! Your answers were sent to «{company}». We will get back to you soon.",
        "uk": "Дякуємо, {name}! Ваші відповіді надіслано до «{company}». Ми зв'яжемося з вами.",
        "ru": "Спасибо, {name}! Ваши ответы отправлены в «{company}». Мы свяжемся с вами.",
    },
    "cancelled": {"it": "Questionario annullato. /start per ricominciare.",
                  "en": "Questionnaire cancelled. /start to begin again.",
                  "uk": "Анкету скасовано. /start — почати знову.",
                  "ru": "Анкета отменена. /start — начать заново."},
}

STATUS = {"new": "Новый", "shortlist": "Шорт-лист", "rejected": "Отказ", "hired": "Нанят",
          "in_progress": "Заполняет", "abandoned": "Бросил"}
ROLE = {"owner": "владелец", "recruiter": "рекрутер"}


def lang_of(code: str | None) -> str:
    short = (code or "")[:2].lower()
    return short if short in LANGS else "en"


def t(lang: str, key: str, **values) -> str:
    variants = BOT[key]
    return variants.get(lang, variants["en"]).format(**values)


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def local_time(ts: float | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return "—" if ts is None else datetime.fromtimestamp(ts, UTC).astimezone(ROME).strftime(fmt)
