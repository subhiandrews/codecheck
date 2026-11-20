from codecheck.config import RoutingConfig
from codecheck.core.diffmap import parse_diff
from codecheck.core.routing import route

CFG = RoutingConfig()


def _mod(path, added, removed=(), start=1, ctx=()):
    """A modified-file diff: optional leading context, then removed and added lines."""
    n_old = len(ctx) + len(removed)
    n_new = len(ctx) + len(added)
    body = "".join(f" {c}\n" for c in ctx) + "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},{n_old} +{start},{n_new} @@\n{body}")


def _new(path, lines):
    body = "".join(f"+{ln}\n" for ln in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def go(*diffs, cfg=CFG):
    parsed = parse_diff("".join(diffs))
    kinds = {".py": "python", ".sql": "sql", ".js": "javascript", ".java": "java"}
    langs = {p: kinds.get(p[p.rfind("."):]) for p in parsed}
    return route(parsed, langs, cfg)


def test_small_change_skips_architect():
    r = go(_mod("src/a.py", ["x = 2"], ["x = 1"]))
    assert not r.architect and not r.performance and not r.docs


def test_big_or_wide_or_structural_changes_keep_architect():
    assert go(_mod("a.py", [f"x{i} = 1" for i in range(41)])).architect
    assert go(*[_mod(f"f{i}.py", ["x = 2"], ["x = 1"]) for i in range(4)]).architect
    assert go(_new("b.py", ["x = 1"])).architect
    assert go(_mod("pyproject.toml", ["a = 1"], ["a = 2"])).architect
    assert go(_mod("api/schema.proto", ["a"], ["b"])).architect


def test_routing_off_keeps_architect_and_adds_nothing():
    r = go(_mod("src/a.py", ["for i in x:", "    for j in y:", "        f(i, j)"]), cfg=RoutingConfig(mode="off"))
    assert r.architect and not r.performance and not r.docs


def test_nested_new_loop_routes_performance():
    r = go(_mod("src/a.py", ["    for j in y:", "        f(i, j)"], ctx=["for i in x:"]))
    assert r.performance and any("nested loop" in s for s in r.reasons)


def test_query_inside_a_loop_routes_performance():
    r = go(_mod("src/a.py", ["for u in users:", "    db.execute(q, u)"]))
    assert r.performance


def test_unbounded_read_and_cache_route_performance():
    assert go(_mod("src/a.py", ["data = open(p).read()", "@lru_cache(maxsize=None)"])).performance


def test_one_plain_loop_does_not_route_performance():
    assert not go(_mod("src/a.py", ["for i in x:", "    total += i"])).performance


def test_sql_files_score_and_comments_do_not():
    assert go(_new("db/001.sql", ["CREATE INDEX i ON t(a);", "ALTER TABLE t ADD c int;"])).performance
    assert not go(_mod("src/a.py", ["# for i in x: db.execute(q)", "# for j in y:"])).performance


def test_doc_file_or_comment_excess_routes_docs():
    assert go(_mod("README.md", ["new line"])).docs
    noisy = ["# why a", "# why b", "# why c", "x = 1", "y = 2"]
    assert go(_mod("src/a.py", noisy)).docs
    quiet = ["x = 1"] * 40 + ["# why"]
    assert not go(_mod("src/a.py", quiet)).docs
    assert not go(_mod("src/a.py", ["# a", "# b", "x = 1"] + ["y = 1"] * 100)).docs  # under the cap


def test_reasons_are_recorded():
    r = go(_new("a.py", ["x = 1"]))
    assert [x.split(":")[0] for x in r.reasons] == ["architect", "performance", "docs"]


def test_text_files_alone_do_not_route_docs():
    assert not go(_mod("requirements.txt", ["flask==3.0"], ["flask==2.0"])).docs


def test_cap_compares_exact_ratio_not_the_rounded_percent():
    r = go(_new("a.py", ["# a", "# b", "# c"] + ["y = 1"] * 96))  # 3 of 99 = 3.03%
    assert r.doc_metrics.over_cap and r.docs


def test_common_manifests_pipelines_and_interfaces_keep_the_architect():
    for path in ("requirements.txt", "requirements-dev.txt", "Jenkinsfile", ".circleci/config.yml", "app/App.csproj",
                 "Dockerfile.prod", "api.Dockerfile", "include/api.h", "types/api.d.ts", "db/migrate/20240101_add.rb",
                 "go.sum", "makefile"):
        r = go(_mod(path, ["x"], ["y"]))
        assert r.architect, path
    assert not go(_mod("src/a.py", ["x"], ["y"])).architect


def test_one_line_loop_bodies_score_in_brace_and_colon_languages():
    assert go(_new("a.js", ["for (const u of users) { db.query(u); }"])).performance
    assert go(_new("A.java", ["for (User u : users) { repo.findAll(u); }"])).performance
    assert go(_new("a.py", ["for u in users: db.execute('SELECT 1', u)"])).performance
    assert not go(_new("a.js", ["for (const u of users) { total += u.n; }"])).performance


def test_nested_brace_loops_and_tab_indents_are_seen():
    js = ["for (const u of users) {", "  for (const o of orders) {", "    db.query(u, o);", "  }", "}"]
    assert go(_new("a.js", js)).performance
    tabs = ["for u in users:", "\tfor o in orders:", "\t\tdb.execute('SELECT 1')"]
    assert go(_new("a.py", tabs)).performance
    mixed = ["for u in users:", "    for o in orders:", "\t\tdb.execute('SELECT 1')"]
    assert go(_new("b.py", mixed)).performance


def test_comprehension_match_is_bounded_so_long_lines_cannot_backtrack_far():
    from codecheck.core.routing import _LOOP_CALL
    assert _LOOP_CALL.search("y = [x for x in xs]")
    assert not _LOOP_CALL.search("y = [" + "x" * 500 + " for x in xs]")  # past the 200-character window


WARRANTED = {
    "py list membership in a loop": ("a.py", [
        "def dedupe(items):", "    seen = []", "    out = []", "    for x in items:", "        if x in seen:",
        "            continue", "        seen.append(x)", "        out.append(x)", "    return out"]),
    "py blocking call in async code": ("a.py", [
        "async def fetch(url):", "    time.sleep(1)", "    return await client.get(url)"]),
    "js sequential await in a loop": ("a.js", [
        "async function load(ids) {", "  const out = [];", "  for (const id of ids) {", "    out.push(await load(id));",
        "  }", "  return out;", "}"]),
    "js blocking sync call in async code": ("a.js", [
        "async function read(p) {", "  const text = fs.readFileSync(p, 'utf8');", "  return text;", "}"]),
    "py regex compiled every iteration": ("a.py", [
        "for line in lines:", "    pat = re.compile(r'^a+$')", "    if pat.match(line):", "        hits += 1"]),
    "java regex compiled every iteration": ("A.java", [
        "for (String s : items) {", "  Pattern p = Pattern.compile(\"^a+$\");", "  if (p.matcher(s).find()) { n++; }", "}"]),
    "py query in a loop": ("a.py", ["for u in users:", "    rows = db.execute('SELECT 1', u)"]),
    "js nested loops": ("a.js", [
        "for (const a of xs) {", "  for (const b of ys) {", "    out.push(a * b);", "  }", "}"]),
    "py unbounded read in a loop": ("a.py", ["for p in paths:", "    data = open(p).read()"]),
}

NOT_WARRANTED = {
    "py set membership in a loop": ("a.py", [
        "def dedupe(items):", "    seen = set()", "    for x in items:", "        if x in seen:", "            continue",
        "        seen.add(x)"]),
    "py async function that only awaits once": ("a.py", [
        "async def fetch(url):", "    return await client.get(url)"]),
    "js await outside a loop": ("a.js", ["async function f(id) {", "  const r = await load(id);", "  return r;", "}"]),
    "py plain loop over a few values": ("a.py", ["for name in names:", "    print(name)"]),
    "py compile once outside a loop": ("a.py", ["PAT = re.compile(r'^a+$')", "def ok(s):", "    return PAT.match(s)"]),
    "py comment that mentions a loop and sleep": ("a.py", ["# for x in xs: time.sleep(1)  (why we do not loop)", "y = 1"]),
    "py dict membership on a parameter in a loop": ("a.py", [
        "def count(words, table):", "    for w in words:", "        if w in table:", "            print(w)"]),
    "py membership outside a loop": ("a.py", ["def f(x, items):", "    return x in items"]),
}


def test_performance_is_routed_for_every_warranted_situation():
    missed = [name for name, (path, lines) in WARRANTED.items() if not go(_new(path, lines)).performance]
    assert not missed, missed


def test_performance_is_not_routed_for_ordinary_code():
    noisy = [name for name, (path, lines) in NOT_WARRANTED.items() if go(_new(path, lines)).performance]
    assert not noisy, noisy


def test_test_files_never_earn_the_performance_reviewer():
    nested = ["for a in xs:", "    for b in ys:", "        db.execute('SELECT 1', a, b)"]
    assert go(_new("src/a.py", nested)).performance
    for path in ("tests/test_a.py", "test/a.js", "a_test.py", "src/a.spec.js", "pkg/__tests__/a.js"):
        assert not go(_new(path, nested)).performance, path


def test_reads_pile_up_no_score_beyond_one_hint():
    reads = [f"t{i} = p{i}.read_text()" for i in range(6)]
    alone = go(_new("a.py", reads))
    assert not alone.performance and "score 2" in alone.reasons[1]  # six reads count once
    assert "score 3" in go(_new("a.py", reads + ["for x in xs:", "    print(x)"])).reasons[1]


def test_membership_on_an_unknown_name_is_a_weak_hint_and_a_known_list_a_strong_one():
    unknown = go(_new("a.py", ["def f(xs, d):", "    for x in xs:", "        if x in d:", "            pass"]))
    assert "list membership in a loop" in unknown.reasons[1] and not unknown.performance
    known = go(_new("a.py", ["seen = []", "for x in xs:", "    if x in seen:", "        pass"]))
    assert known.performance


def test_long_membership_lines_stay_cheap():
    import time
    line = "    if " + "a and " * 5000 + "b:"
    start = time.perf_counter()
    go(_new("a.py", ["for x in xs:", line]))
    assert time.perf_counter() - start < 5  # generous: a quadratic scan of this line takes minutes
