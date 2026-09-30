"""Точка входа: `python -m hr_saas` или команда `hr-saas`."""


def greet(name: str) -> str:
    return f"Привет, {name}!"


def main() -> None:
    print(greet("мир"))


if __name__ == "__main__":
    main()
