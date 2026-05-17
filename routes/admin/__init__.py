"""Admin routes — split into focused modules aligned with the new admin IA.

Each submodule registers its routes via `@app.route(...)` against the shared
Flask app constructed in `app.py`. We intentionally avoid Flask Blueprints
because templates rely on flat endpoint names (`url_for('dashboard')`,
`url_for('login')`, etc.). Splitting into files gives us the modularity the
admin reorg needs without forcing a 24-template churn.

Submodule mapping (aligns with the new IA we're migrating to):
  - auth       — login / logout
  - work_tasks — dashboard, inbox, leads, issues, logs, conversation, ops APIs
  - business   — business_lines, courses, services, software, products, team
  - bot        — training, training_snippets, handoff_keywords, faq
  - system     — settings, audit_log, docs, admin user management
"""

from . import auth         # noqa: F401
from . import work_tasks   # noqa: F401
from . import business     # noqa: F401
from . import bot          # noqa: F401
from . import system       # noqa: F401
