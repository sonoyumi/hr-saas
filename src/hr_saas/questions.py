"""A questionnaire written as plain text, so a recruiter can edit it in one textarea:

    ? How many years of Python experience?
    - Less than 1 year | 0
    - 1-3 years | 2
    - More than 3 years | 3
    ?? Tell us about your hardest project | python, asyncio, tests, api

`?`  a question with buttons; each `-` line is an option with its points (0-10, default 0).
`??` an open question; after `|` — keywords that a good answer mentions (for scoring).
Empty lines and lines starting with `#` are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_QUESTIONS = 20
MAX_OPTIONS = 8
MAX_TEXT = 300
MAX_OPTION = 60  # Telegram button text stays readable


class QuestionsError(Exception):
    """Every problem with its line number."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class Option:
    text: str
    points: int = 0


@dataclass
class Question:
    text: str
    kind: str  # "choice" | "open"
    options: list[Option] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @property
    def max_points(self) -> int:
        return max((o.points for o in self.options), default=0) if self.kind == "choice" else 10


def parse(source: str) -> list[Question]:
    questions: list[Question] = []
    problems: list[str] = []
    for n, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("??"):
            text, _, kw = line[2:].partition("|")
            text = text.strip()
            keywords = [k.strip().lower() for k in kw.split(",") if k.strip()]
            if not text:
                problems.append(f"строка {n}: у открытого вопроса нет текста")
            questions.append(Question(text[:MAX_TEXT], "open", keywords=keywords[:20]))
        elif line.startswith("?"):
            text = line[1:].strip()
            if not text:
                problems.append(f"строка {n}: у вопроса нет текста")
            questions.append(Question(text[:MAX_TEXT], "choice"))
        elif line.startswith("-"):
            if not questions or questions[-1].kind != "choice":
                problems.append(f"строка {n}: вариант ответа «-» без вопроса «?» над ним")
                continue
            text, sep, pts = line[1:].partition("|")
            text = text.strip()
            points = 0
            if sep:
                try:
                    points = int(pts.strip())
                except ValueError:
                    problems.append(f"строка {n}: баллы должны быть числом, получено {pts.strip()!r}")
                if not 0 <= points <= 10:
                    problems.append(f"строка {n}: баллы от 0 до 10")
            if not text:
                problems.append(f"строка {n}: пустой вариант ответа")
            elif len(text) > MAX_OPTION:
                problems.append(f"строка {n}: вариант длиннее {MAX_OPTION} символов")
            questions[-1].options.append(Option(text, points))
        else:
            problems.append(f"строка {n}: строка должна начинаться с «?», «??» или «-»")
    for i, q in enumerate(questions, 1):
        if q.kind == "choice" and len(q.options) < 2:
            problems.append(f"вопрос {i} «{q.text[:40]}»: нужно хотя бы 2 варианта ответа")
        if len(q.options) > MAX_OPTIONS:
            problems.append(f"вопрос {i}: не больше {MAX_OPTIONS} вариантов")
    if not questions:
        problems.append("нет ни одного вопроса")
    if len(questions) > MAX_QUESTIONS:
        problems.append(f"не больше {MAX_QUESTIONS} вопросов")
    if problems:
        raise QuestionsError(problems)
    return questions


def render(questions: list[Question]) -> str:
    """Back to text for the editor (parse(render(q)) == q)."""
    lines = []
    for q in questions:
        if q.kind == "open":
            lines.append(f"?? {q.text}" + (f" | {', '.join(q.keywords)}" if q.keywords else ""))
        else:
            lines.append(f"? {q.text}")
            lines += [f"- {o.text} | {o.points}" for o in q.options]
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_json(questions: list[Question]) -> list[dict]:
    return [{"text": q.text, "kind": q.kind, "options": [{"text": o.text, "points": o.points} for o in q.options],
             "keywords": q.keywords} for q in questions]


def from_json(data: list[dict]) -> list[Question]:
    return [Question(d["text"], d["kind"], [Option(o["text"], int(o["points"])) for o in d.get("options", [])],
                     list(d.get("keywords", []))) for d in data]
