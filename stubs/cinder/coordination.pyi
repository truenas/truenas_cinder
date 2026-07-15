from collections.abc import Callable
from typing import Any, TypeVar

_F = TypeVar('_F', bound=Callable[..., Any])

class Coordinator:
    started: bool
    def start(self, start_heart: bool = ...) -> None: ...
    def stop(self) -> None: ...
    def get_lock(self, name: str) -> Any: ...

COORDINATOR: Coordinator

def synchronized(
    *lock_names: str,
    blocking: bool = ...,
    coordinator: Any = ...,
) -> Callable[[_F], _F]: ...
