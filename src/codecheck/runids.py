import re
import uuid

from codecheck.errors import CodeCheckError

_RUN_ID = re.compile(r"^[0-9a-f]{12}$")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise CodeCheckError(f"invalid run id {run_id!r}")
    return run_id
