class BridgeError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class InputError(BridgeError, ValueError):
    pass
