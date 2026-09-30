"""Plans: what a company may use. The platform owner changes a plan with `hr-saas set-plan`."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    code: str
    title: str
    price_eur: int
    vacancies: int | None  # active vacancies at once; None = unlimited
    candidates_month: int | None  # finished questionnaires per calendar month
    recruiters: int | None  # users in the cabinet
    ai: bool  # AI scoring of open answers


PLANS = {
    "free": Plan("free", "Free", 0, 1, 30, 1, False),
    "pro": Plan("pro", "Pro", 29, 10, 500, 5, True),
    "business": Plan("business", "Business", 99, None, None, None, True),
}


def plan(code: str) -> Plan:
    return PLANS.get(code, PLANS["free"])


def within(limit: int | None, used: int) -> bool:
    """True if one more item still fits into the limit."""
    return limit is None or used < limit


def usage_percent(limit: int | None, used: int) -> int | None:
    return None if not limit else min(100, round(100 * used / limit))
