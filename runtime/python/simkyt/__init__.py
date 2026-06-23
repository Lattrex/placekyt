from . import simkyt as _ext
from .simkyt import *

__doc__ = _ext.__doc__
__version__ = getattr(_ext, "__version__", "0.1.0")
if hasattr(_ext, "__all__"):
    __all__ = _ext.__all__
