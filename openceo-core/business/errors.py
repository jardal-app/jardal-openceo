from typing import Any


class BusinessError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BusinessUnavailable(BusinessError):
    pass


class BusinessValidationError(BusinessError):
    pass


class BusinessPermissionError(BusinessValidationError):
    pass


class BusinessDuplicateError(BusinessError):
    pass


class BusinessDependencyBlockedError(BusinessValidationError):
    pass


class BusinessDateBoundError(BusinessValidationError):
    pass


class BusinessVersionConflict(BusinessValidationError):
    pass
