"""EdgePowerMeter application package.

Kept import-light on purpose: subprocesses such as the GPU probe import
`app.*` modules and must not pay for Qt widgets, numpy or reportlab.
"""

from .version import APP_NAME, __version__, __version_info__

__all__ = ["__version__", "__version_info__", "APP_NAME"]
