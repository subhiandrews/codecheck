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
    langs = {p: ("python" if p.endswith(".py") else "sql" if p.endswith(".sql") else None) for p in parsed}
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
