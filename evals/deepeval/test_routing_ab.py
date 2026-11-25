from types import SimpleNamespace

import pytest
from routing_ab import compare, load_routing

from codecheck.config import RoutingConfig
from codecheck.core.routing import route as current_route

CFG = RoutingConfig()


def _diff(path: str, lines: list[str]) -> str:
    body = "".join(f"+{ln}\n" for ln in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def _fake(performance: bool, reason: str = "perf"):
    def route(diffs, languages, cfg):
        return SimpleNamespace(performance=performance, architect=True, docs=False, reasons=("a", reason, "d"))
    return route


def test_a_diff_the_new_routing_turns_off_is_a_flip_and_identical_diffs_count_once():
    diffs = {"r1": _diff("a.py", ["x = 1"]), "r2": _diff("a.py", ["x = 1"]), "r3": _diff("b.py", ["y = 2"])}
    result = compare(diffs, _fake(True, "old: score 4"), _fake(False, "new: score 0"), CFG)
    assert result["summary"]["distinct diffs"] == 2  # r1 and r2 are the same diff
    assert result["summary"]["performance: old=on new=off"] == 2
    assert [f["run"] for f in result["flips"]] == ["r1", "r3"]
    assert result["flips"][0]["old_reason"] == "old: score 4" and result["flips"][0]["new_reason"] == "new: score 0"


def test_no_difference_means_no_flips_and_a_broken_diff_is_counted_not_fatal():
    diffs = {"ok": _diff("a.py", ["x = 1"])}
    same = compare(diffs, _fake(True), _fake(True), CFG)
    assert same["flips"] == [] and same["summary"]["performance: old=on new=on"] == 1

    def boom(diffs, languages, cfg):
        raise RuntimeError("bad")

    broken = compare(diffs, boom, _fake(True), CFG)
    assert broken["summary"]["errors"] == 1 and broken["summary"]["distinct diffs"] == 0


def test_the_same_routing_on_both_sides_never_flips():
    diffs = {"loop": _diff("a.py", ["for x in xs:", "    for y in ys:", "        pass"]), "small": _diff("b.py", ["z = 1"])}
    result = compare(diffs, current_route, current_route, CFG)
    assert result["summary"]["distinct diffs"] == 2 and result["flips"] == [] and result["first_error"] is None


def test_a_ref_that_holds_a_routing_module_loads_with_a_route_function():
    assert callable(load_routing("HEAD").route)


def test_a_failing_old_routing_is_reported_with_its_first_error():
    def boom(diffs, languages, cfg):
        raise RuntimeError("old routing cannot run here")

    result = compare({"r1": _diff("a.py", ["x = 1"])}, boom, _fake(True), CFG)
    assert result["summary"]["errors"] == 1 and "old routing cannot run here" in result["first_error"]["error"]


def test_a_routing_with_fewer_reasons_is_read_without_crashing():
    def short(diffs, languages, cfg):  # `route` returns one reason when routing is off
        return SimpleNamespace(performance=False, architect=True, docs=False, reasons=("off",))

    result = compare({"r1": _diff("a.py", ["x = 1"])}, short, short, CFG)
    assert result["summary"]["errors"] == 0 and result["rows"][0]["old_reason"] == ""


@pytest.mark.parametrize("ref", ["", "-p", "--output=x"])
def test_a_ref_that_could_act_as_a_git_option_is_refused(ref):
    with pytest.raises(ValueError):
        load_routing(ref)
