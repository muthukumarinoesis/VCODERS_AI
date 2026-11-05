class RunnerError(Exception):
    """A job failure the client can show: `code` is stable, `message` says what to do."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        self.code, self.message = code, message
