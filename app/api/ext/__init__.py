"""Extension tools for the automation API.

An extension adds tools without touching the core files. For a name `foo`:

- `app/api/ext/foo_schema.py` (stdlib only; the MCP bridge imports it): defines
  `TOOLS: List[schema.Tool]` built with the public helpers of `app.api.schema` (`tool`,
  `number`, `integer`, `boolean`, `string`, `enum`, `quantities`, `range_props`,
  `object_schema`). Optional `TIMEOUTS: Dict[str, str]` maps a tool name to `"long"`
  (600 s) or `"param:<arg>"` (that numeric argument + 60 s, capped).
- `app/api/ext/foo.py` (may import numpy and app.core): defines
  `handlers(service) -> Dict[str, Callable[..., dict]]`. Each callable receives the
  validated arguments as keyword arguments and returns a JSON-able dict. To return an
  image, return `"image": {"mime_type": "image/png", "data_base64": "..."}` (the bridge
  turns it into an MCP image block). The service methods an extension may use are listed
  in the `ApiService` docstring (`app/api/service.py`).

Then add `"foo"` to `EXTENSIONS` below. Tool names must be unique across the built-in
tools and all extensions; a clash fails at import time.

Importing `app.api.schema` (directly or through the MCP bridge) loads the schema modules
of every extension in `EXTENSIONS`. Import `app.api.schema` before an extension's
`_schema` module.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import List

# Extension names, in load order: each one needs `<name>_schema.py` and `<name>.py`.
EXTENSIONS: List[str] = ["analysis", "plot", "triggers", "runs", "events"]


def schema_modules() -> List[ModuleType]:
    """Import every `<name>_schema` module. Stdlib-only: safe for the MCP bridge."""
    return [importlib.import_module("{}.{}_schema".format(__name__, name)) for name in EXTENSIONS]


def handler_modules() -> List[ModuleType]:
    """Import every `<name>` handler module (may import numpy and app.core)."""
    return [importlib.import_module("{}.{}".format(__name__, name)) for name in EXTENSIONS]
