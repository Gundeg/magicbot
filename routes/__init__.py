"""Route package. Importing this module registers all HTTP routes against the
Flask app created in `app.py`.

Order matters: each submodule decorates `@app.route(...)`, so `app` must be
fully constructed before this package is imported.
"""

from . import public      # noqa: F401  — registers /, /privacy
from . import webhook     # noqa: F401  — registers /webhook
from . import admin       # noqa: F401  — registers all /admin/* routes
