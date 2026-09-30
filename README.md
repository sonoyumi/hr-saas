# hr-saas

Короткое описание: что делает проект и зачем.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # заполнить своими значениями
hr-saas           # или: python -m hr_saas
```

## Тесты и линтер

```bash
pytest
ruff check .
```

## Структура

```
src/hr_saas/   код
tests/              тесты
```
