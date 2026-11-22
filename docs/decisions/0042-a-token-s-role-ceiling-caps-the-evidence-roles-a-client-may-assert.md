# 0042 A token's role ceiling caps the evidence roles a client may assert
Status: accepted 2026-11-21
Decision: Each memory token carries a ceiling, contributor by default or maintainer with --maintainer. The server downgrades any claimed maintainer evidence above the ceiling before storing it, reports the count, and keeps per-contributor roles so a purge lowers them. Solo mode keeps its own roles.
Why: A role is the only evidence weight a client could inflate; trusting the claim let any teammate activate a lesson.
Rejected: Trusting claimed roles; verifying roles against the host from the server, which needs host credentials there.
