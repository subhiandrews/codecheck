# Review guidance for code-check

1. Look for duplication across `skills/*/SKILL.md`: repeated step sequences, near-identical prose and copied rules. Suggest extracting what is shared.
2. Check that changed or added tests are valid. Each test must be able to fail when the behaviour it covers breaks. Flag these tautologies:
   - `assert True`, or `x == x`.
   - A value asserted against the same expression or constant the code just returned.
   - A test that asserts only that a mock was called with what the test itself set up.
   - An assertion that cannot fail, such as an `or` with a branch that is always true, or a bare truthiness check on a non-empty container.
   - A test with no assertion at all.

   Also flag tests that mutate or stub away the very code under test.
3. Check that the plugin version is bumped when the PR changes what an installed plugin ships. Installed copies only update when the version changes, so a change without a bump never reaches users. A bump is needed when the PR touches `skills/`, `agents/` (including prompt or default-model changes in `src/codecheck/agentgen.py`), `hooks/`, `bin/`, `.claude-plugin/`, `installer/`, `codex/`, `src/codecheck/commands/panel.py`, or the CLI under `src/codecheck/` in a way that changes its behaviour. Docs, tests, CI and golden-case changes need none.
   - The version must be higher than the base branch's and identical in every place it lives: `VERSION`, `pyproject.toml`, `package.json`, `package-lock.json` (twice), `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and `src/codecheck/__init__.py`. Flag a PR that bumps some of these but not all.
   - Flag a missing bump as a Warning, and say which changed paths need it.
4. Check that the documentation matches the PR and keeps its structure. `README.md` is the short, current guide; design detail lives under `docs/`.
   - **Currency.** Flag a PR that adds, removes or changes a skill, an agent, a CLI subcommand or flag, a config key or its default, a supported code host (GitHub, GitLab) or runtime (Claude Code, Codex), or an install step, without updating `README.md` in the same PR. Settings also belong in `docs/CONFIGURATION.md`. Design, flow, safety-model and GitLab-difference changes belong in `docs/ARCHITECTURE.md`. Maintainer tooling (shadow mode, hand-offs, golden cases, evals, gates) belongs in `docs/MAINTAINING.md`.
   - **Truth.** Flag any README claim that the diff makes false, for example a support-matrix cell, an install command, a default model or a skill description. Name the line.
   - **Size and scope.** Flag a `README.md` over 260 lines. Flag architecture or design prose, long config reference or maintainer tooling added to the README; it should be a short summary with a link to the `docs/` file.
   - **Structure.** The README must keep: what it is, the host and runtime support matrix, install for Claude Code and Codex and for GitHub and GitLab, quick start, the skills, the reviewer agents, the CLI, configuration, safety, and links to `docs/`.
   - Flag each missing update as a Warning and say which doc and which change it follows from. A PR that changes only tests, CI or golden cases needs none.
