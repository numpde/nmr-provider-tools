"""Authored failures from add-on evidence checks; the raw core uses ordinary errors."""


class IdentityError(ValueError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
