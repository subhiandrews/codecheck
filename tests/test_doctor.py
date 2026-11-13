from codecheck.commands.doctor import python_check


def test_python_floor_warns_below_3_14_10():
    assert python_check((3, 14, 8, "final", 0))["ok"] is False
    assert python_check((3, 14, 10, "final", 0))["ok"] is True
    assert python_check((3, 15, 0, "final", 0))["ok"] is True
    assert python_check((3, 14, 8, "final", 0))["required"] is False
