# Review guidance for code-check

1. Look for duplication across `skills/*/SKILL.md`: repeated step sequences, near-identical prose and copied rules. Suggest extracting what is shared.
2. Check that changed or added tests are valid. Each test must be able to fail when the behaviour it covers breaks. Flag these tautologies:
   - `assert True`, or `x == x`.
   - A value asserted against the same expression or constant the code just returned.
   - A test that asserts only that a mock was called with what the test itself set up.
   - An assertion that cannot fail, such as an `or` with a branch that is always true, or a bare truthiness check on a non-empty container.
   - A test with no assertion at all.

   Also flag tests that mutate or stub away the very code under test.
