"""THROWAWAY: a deliberately failing test used to reproduce #106 (babysit-pr and a failing CI check). Never merge."""


def add(a: int, b: int) -> int:
    return a + b


def test_add_adds_two_numbers():
    assert add(2, 3) == 5
