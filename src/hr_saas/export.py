"""Excel export of one company's candidates: a summary sheet and one row per answer.

Candidates' texts are untrusted: a cell starting with = + - @ would become a live formula in the
recruiter's Excel (formula injection), so such values are written as text.
"""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

from . import texts

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe(value):
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if value.startswith(FORMULA_START):
            return "'" + value
    return value


def build(rows) -> bytes:
    wb = Workbook()
    summary = wb.active
    summary.title = "Кандидаты"
    detail = wb.create_sheet("Ответы")
    summary.append(["№", "Кандидат", "Telegram", "Вакансия", "Статус", "Балл", "Анкета заполнена"])
    detail.append(["№", "Кандидат", "Вопрос", "Ответ", "Баллы", "Из", "Комментарий"])
    seen = set()
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            summary.append([r["id"], safe(r["name"]), safe("@" + r["username"] if r["username"] else ""),
                            safe(r["vacancy"]), texts.STATUS.get(r["status"], r["status"]), r["score"],
                            texts.local_time(r["finished_at"])])
        if r["q_index"] is not None:
            detail.append([r["id"], safe(r["name"]), safe(r["question"]), safe(r["answer"]), r["points"],
                           r["max_points"], safe(r["comment"] or "")])
    for ws, widths in ((summary, (6, 24, 18, 28, 12, 8, 18)), (detail, (6, 24, 40, 60, 8, 6, 40))):
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
