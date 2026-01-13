from . import calib
from . import preproc
from . import postproc
from . import utils

def __getattr__(name: str):
    if name == '__version__':
        from importlib.metadata import version
        return version('vip_hci')
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")