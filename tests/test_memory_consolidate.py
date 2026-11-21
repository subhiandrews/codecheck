from codecheck.memory.categories import CANONICAL, canonical_category
from codecheck.memory.consolidate import (
    DAY,
    WEIGHTS,
    Params,
    consolidate,
    dedupe,
    effective_weight,
)
from codecheck.memory.model import Evidence, Scope, evidence_id, lesson_id

T0 = 1_700_000_000.0
SCOPE = Scope("github.com", "o/r", "src/app/**", "python", "tests")
MISS_SCOPE = Scope("github.com", "o/r", "src/app/**", "python", "any")


def ev(signal, pr, thread, ts=T0, scope=SCOPE):
    polarity, weight = WEIGHTS[signal]
    kind = "miss" if polarity == "miss" else "confirm" if polarity == "for" else "dispute"
    return Evidence(evidence_id("github.com", scope.repo, pr, thread, signal), lesson_id(scope, kind), scope, polarity, signal,
                    weight, ts, pr)


def test_ids_are_stable_and_depend_on_scope_and_kind():
    assert lesson_id(SCOPE, "dispute") == lesson_id(Scope(*SCOPE.key()), "dispute")
    assert lesson_id(SCOPE, "dispute") != lesson_id(SCOPE, "confirm")
    assert lesson_id(SCOPE, "dispute") != lesson_id(Scope("github.com", "o/other", *SCOPE.key()[2:]), "dispute")
    assert evidence_id("h", "r", "r#1", "T1", "human_miss") == evidence_id("h", "r", "r#1", "T1", "human_miss")
    assert evidence_id("h", "r", "r#1", "T1", "human_miss") != evidence_id("h", "r", "r#1", "T2", "human_miss")
    assert len(lesson_id(SCOPE, "miss")) == 16


def test_the_same_thread_collected_twice_is_one_row():
    a, b = ev("human_miss", "r#1", "T1", T0), ev("human_miss", "r#1", "T1", T0 + 5)
    assert len(dedupe([a, b, a])) == 1
    (lesson,) = consolidate([a, b], T0 + 10)
    assert lesson.support == 1.0


def test_two_prs_reach_activation_and_one_pr_does_not():
    two = consolidate([ev("verdict_valid", "r#1", "A"), ev("verdict_valid", "r#2", "B")], T0 + DAY)
    assert [(le.kind, le.status, le.n_prs) for le in two] == [("confirm", "active", 2)]
    one = consolidate([ev("verdict_valid", "r#1", "A"), ev("verdict_valid", "r#1", "B")], T0 + DAY)
    assert one[0].status == "candidate" and one[0].support == 2.0 and one[0].n_prs == 1
    weak = consolidate([ev("resolved", "r#1", "A"), ev("resolved", "r#2", "B")], T0 + DAY)
    assert weak[0].status == "candidate"  # 0.25 each


def test_a_small_minority_does_not_contest():
    items = [ev("verdict_valid", "r#1", "A"), ev("verdict_valid", "r#2", "B"), ev("verdict_valid", "r#4", "D"),
             ev("verdict_incorrect", "r#3", "C")]
    (lesson,) = consolidate(items, T0 + DAY)
    assert lesson.support == 3.0 and lesson.contra == 1.0 and lesson.status == "active"  # 1.0 is under half of 3.0


def test_contested_when_minority_is_at_least_half():
    items = [ev("verdict_valid", "r#1", "A"), ev("verdict_valid", "r#2", "B"), ev("verdict_incorrect", "r#3", "C"),
             ev("verdict_incorrect", "r#4", "D")]
    (lesson,) = consolidate(items, T0 + DAY)
    assert lesson.status == "contested"


def test_replies_are_neutral_and_alone_make_no_lesson():
    assert consolidate([ev("reply", "r#1", "A")], T0 + DAY) == []


def test_decay_halves_at_90_days_and_expires_at_180_with_an_injected_clock():
    e = ev("verdict_valid", "r#1", "A")
    p = Params()
    assert effective_weight(e, T0 + 89 * DAY, p) == 1.0
    assert effective_weight(e, T0 + 90 * DAY, p) == 0.5
    assert effective_weight(e, T0 + 179 * DAY, p) == 0.5
    assert effective_weight(e, T0 + 180 * DAY, p) == 0.0
    assert consolidate([e], T0 + 100 * DAY)[0].support == 0.5
    (lesson,) = consolidate([e], T0 + 200 * DAY)
    assert lesson.status == "expired" and lesson.expires_ts == T0 + 180 * DAY
    shorter = Params(half_days=10, expire_days=20)
    assert effective_weight(e, T0 + 10 * DAY, shorter) == 0.5 and effective_weight(e, T0 + 20 * DAY, shorter) == 0.0


def test_decay_can_stop_a_lesson_from_activating():
    items = [ev("verdict_valid", "r#1", "A"), ev("verdict_valid", "r#2", "B")]
    assert consolidate(items, T0 + 10 * DAY)[0].status == "active"
    assert consolidate(items, T0 + 100 * DAY)[0].status == "candidate"  # 0.5 + 0.5


def test_evidence_from_the_future_is_ignored():
    assert consolidate([ev("human_miss", "r#1", "A", ts=T0 + 10)], T0) == []


def test_miss_and_finding_scopes_stay_separate_and_output_is_sorted():
    lessons = consolidate([ev("human_miss", "r#1", "A", scope=MISS_SCOPE), ev("verdict_valid", "r#1", "B")], T0 + 1)
    assert sorted(le.kind for le in lessons) == ["confirm", "miss"]
    assert [le.id for le in lessons] == sorted(le.id for le in lessons)
    assert all(le.unweighted for le in lessons)


def test_category_aliases():
    assert canonical_category("test") == canonical_category("tests") == canonical_category("Test Coverage") == "tests"
    assert canonical_category("error handling") == canonical_category("error-handling") == "error-handling"
    assert canonical_category("Edge_Case") == "correctness"
    assert canonical_category("something odd") == "other"
    assert canonical_category(None) == "other"
    assert {canonical_category(c) for c in CANONICAL} == set(CANONICAL)
