class ModelNotFoundError(Exception):
    """Raised when a Groq model ID is invalid or not available on the account."""
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
 
 
class ModelNotSpecifiedError(Exception):
    """Raised when no model ID is provided to a service that requires one."""
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
 