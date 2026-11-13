class CodeCheckError(Exception):
    """Base class; the CLI turns these into {"error": ...} JSON and exit code 1."""


class ConfigError(CodeCheckError): ...


class GitError(CodeCheckError): ...


class SyncError(CodeCheckError): ...


class ProviderError(CodeCheckError): ...
