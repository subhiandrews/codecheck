"""Who said it: the writer's role on a repository, recorded next to feedback so evidence can be weighted later."""

ROLES = ("maintainer", "author", "contributor", "self", "unknown")
# GitHub author associations. MEMBER only means an organisation member, not write access, so it is left out: a
# MEMBER falls through to `unknown`. COLLABORATOR is not write access either (it may be read or triage only), and
# `maintainer` is the one role that can activate a lesson, so it too falls to `contributor`. These are a fallback for
# when `can_write` is unavailable, never a replacement.
_WRITE_ASSOCIATIONS = frozenset({"OWNER"})
_OUTSIDE_ASSOCIATIONS = frozenset({"COLLABORATOR", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN"})


def derive_role(login: str, *, me: str, pr_author: str | None, can_write: bool | None, association: str = "") -> str:
    """The tool's own login is `self`, then the PR author is `author` (whatever access they hold: an author's word
    on their own PR is not independent). Otherwise write access decides, falling back to the host's association."""
    if login == me:
        return "self"
    if pr_author and login == pr_author:
        return "author"
    if can_write is True:
        return "maintainer"
    if can_write is False:
        return "contributor"
    assoc = (association or "").upper()
    if assoc in _WRITE_ASSOCIATIONS:
        return "maintainer"
    if assoc in _OUTSIDE_ASSOCIATIONS:
        return "contributor"
    return "unknown"


class RoleResolver:
    """Derives roles for one repository, asking the host at most once per login. Never raises."""

    def __init__(self, provider, repo: str, me: str, pr_author: str | None) -> None:
        self._provider, self._repo, self._me, self._pr_author = provider, repo, me, pr_author
        self._cache: dict[tuple[str, str], bool | None] = {}

    def _can_write(self, login: str) -> bool | None:
        key = (self._repo, login)
        if key not in self._cache:
            fn = getattr(self._provider, "can_write", None)
            try:
                value = fn(self._repo, login) if fn else None
            except Exception:  # noqa: BLE001 - recording must never fail because the host would not say
                value = None
            self._cache[key] = value if isinstance(value, bool) else None
        return self._cache[key]

    def role(self, login: str, association: str = "") -> str:
        if login == self._me or (self._pr_author and login == self._pr_author):  # no lookup needed
            return derive_role(login, me=self._me, pr_author=self._pr_author, can_write=None, association=association)
        return derive_role(login, me=self._me, pr_author=self._pr_author, can_write=self._can_write(login),
                           association=association)
