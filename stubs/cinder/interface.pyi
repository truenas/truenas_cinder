from typing import TypeVar

_T = TypeVar('_T')

def volumedriver(cls: type[_T]) -> type[_T]: ...
