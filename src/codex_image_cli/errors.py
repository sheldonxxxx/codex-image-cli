class CliError(Exception):
    """Only curated, non-secret messages may cross the CLI boundary."""

    def __init__(self, message: str, *, kind: str = "validation", exit_code: int = 2):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code


RENEW_SESSION = "Run Codex once to renew your ChatGPT session, then retry manually."
