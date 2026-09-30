from hr_saas.__main__ import greet


def test_greet():
    assert greet("Python") == "Привет, Python!"
