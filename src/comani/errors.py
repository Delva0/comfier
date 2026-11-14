"""Errors shared by the lightweight client and the daemon protocol."""


class ComaniError(Exception):
    code = "ComaniError"

    def __init__(self, message: str = "", *, details=None):
        super().__init__(message)
        self.details = details


class SessionClosed(ComaniError):
    code = "SessionClosed"


class InvalidReference(ComaniError):
    code = "InvalidReference"


class NotSubmitted(InvalidReference):
    code = "NotSubmitted"


class Expired(InvalidReference):
    code = "Expired"


class Released(InvalidReference):
    code = "Released"


class NotProduced(InvalidReference):
    code = "NotProduced"


class Skipped(NotProduced):
    code = "Skipped"


class UnknownOutputLayout(ComaniError):
    code = "UnknownOutputLayout"


class CodeCompatibilityError(ComaniError):
    code = "CodeCompatibilityError"


class PackageConflict(ComaniError):
    code = "PackageConflict"


class RemoteError(ComaniError):
    code = "RemoteError"


_ERROR_TYPES = {cls.code: cls for cls in (
    ComaniError, SessionClosed, InvalidReference, NotSubmitted, Expired,
    Released, NotProduced, Skipped, UnknownOutputLayout,
    CodeCompatibilityError, PackageConflict, RemoteError,
)}


def error_from_wire(error):
    if isinstance(error, str):
        return RemoteError(error)
    error = error or {}
    cls = _ERROR_TYPES.get(error.get("code"), RemoteError)
    return cls(error.get("message", str(error)), details=error)


def error_to_wire(error):
    return {"code": getattr(error, "code", type(error).__name__), "message": str(error)}
