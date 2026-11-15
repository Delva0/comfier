"""Errors shared by the lightweight client and the daemon protocol."""


class ComaniError(Exception):
    code = "ComaniError"

    def __init__(self, message: str = "", *, details=None):
        super().__init__(message)
        self.details = details


class SessionClosed(ComaniError):
    code = "SessionClosed"


class SessionExpired(SessionClosed):
    code = "SessionExpired"


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


class FileTransferExpired(RemoteError):
    """A file receipt is unknown, expired, cancelled, or from another daemon."""

    code = "FileTransferExpired"


class FileTransferBusy(RemoteError):
    """File admission is full; retrying the same unaccepted identity is safe."""

    code = "FileTransferBusy"


class SubmissionKeyConflict(RemoteError):
    code = "SubmissionKeyConflict"


class CallExpired(Expired):
    code = "CallExpired"


class OperationError(RemoteError):
    """A management operation failed; completed changes are not rolled back."""

    code = "OperationError"

    @property
    def result(self):
        return self.details.get("result") if isinstance(self.details, dict) else None


class OperationCancelled(OperationError):
    code = "OperationCancelled"


class OperationTimeout(OperationError):
    code = "OperationTimeout"


class OperationUnknown(OperationError):
    code = "OperationUnknown"


class OperationExpired(OperationUnknown):
    code = "OperationExpired"


class DaemonRestarted(OperationUnknown):
    code = "DaemonRestarted"


class LogExpired(OperationError):
    code = "LogExpired"


class CommandError(OperationError):
    code = "CommandError"


_ERROR_TYPES = {cls.code: cls for cls in (
    ComaniError, SessionClosed, SessionExpired, InvalidReference, NotSubmitted, Expired,
    Released, NotProduced, Skipped, UnknownOutputLayout,
    CodeCompatibilityError, PackageConflict, RemoteError, SubmissionKeyConflict, CallExpired,
    OperationError, OperationCancelled, OperationTimeout, OperationUnknown, OperationExpired,
    DaemonRestarted, LogExpired, CommandError, FileTransferExpired, FileTransferBusy,
)}
_ERROR_TYPES["RefExpired"] = Expired


def error_from_wire(error):
    if isinstance(error, str):
        return RemoteError(error)
    error = error or {}
    cls = _ERROR_TYPES.get(error.get("code"), RemoteError)
    return cls(error.get("message", str(error)), details=error)


def error_to_wire(error):
    details = getattr(error, "details", None)
    return {**(details if isinstance(details, dict) else {}),
            "code": getattr(error, "code", type(error).__name__), "message": str(error)}
