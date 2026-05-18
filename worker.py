"""Long-running worker entry point — background loops only, no web server.

Procfile entry: `worker: WORKER_ROLE=worker python -u worker.py`

Importing `app` triggers the same env-driven thread spawns (polling_task,
nudge_task, cluster_task) that the web dyno used to host. By isolating them
on a dedicated dyno we avoid duplicating background work across gunicorn
workers, which is what the WORKER_ROLE gating in `app.py` is for.

This process blocks forever on a sleep loop so the worker daemon threads
stay alive. There is no HTTP server here; healthchecks (if any) should hit
the web dyno.
"""
import logging
import os
import time

# Force the role so background loops actually start even if the operator
# forgot to set the env var in the dyno definition.
os.environ.setdefault('WORKER_ROLE', 'worker')

import app  # noqa: F401 — importing spawns the gated background threads

logger = logging.getLogger(__name__)
logger.info('Worker process started; background loops attached.')

# Keep the main thread alive so the daemon threads we just spawned can run.
while True:
    time.sleep(3600)
