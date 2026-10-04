"""Domain error raised for semantic fixture-validation failures.

Mapped to HTTP 422 by the FastAPI exception handler so the response shape
matches Pydantic's own ``RequestValidationError`` responses.
"""


class FixtureValidationError(Exception):
    def __init__(self, details: list[dict]):
        self.details = details
        super().__init__(f"fixture validation failed with {len(details)} error(s)")
