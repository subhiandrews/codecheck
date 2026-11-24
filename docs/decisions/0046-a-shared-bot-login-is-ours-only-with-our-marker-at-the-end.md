# 0046 A shared bot login is ours only with our marker at the end
Status: accepted 2026-11-23
Decision: A comment or review by a bot login is ours only if its body ends with one of our hidden markers. The bare GraphQL login matches name[bot] only when the host flags a bot. A PR by app/name is our own PR.
Why: Every workflow using the default token posts as github-actions. A marker narrows that doubt; it cannot close it.
Rejected: Login alone, or a marker anywhere in the body: other workflows' comments and quoted text would count.
Evidence: tests/test_bot_login_routes.py
