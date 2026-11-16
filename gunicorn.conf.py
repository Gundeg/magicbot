"""Gunicorn settings — loaded automatically from the working directory (the
Render start command `gunicorn app:app --workers 2 --timeout 60` picks this
file up without any flag; command-line options still win).

Diagnostic for the intermittent WORKER TIMEOUTs (2026-11-16): a request stuck
for 60 s gets the worker SIGKILLed one second after SIGABRT, so it never ran
Python again — a Python-level `worker_abort` hook would print nothing. Instead,
`faulthandler`'s watchdog thread is armed when each request starts and
cancelled when it ends; if a request runs past STACK_DUMP_AFTER_SECONDS it
writes EVERY thread's stack to stderr (Render logs), even when the stuck call
is inside C code (sqlite, sockets). Normal requests log nothing. Search the
Render logs for "Thread 0x" or "most recent call first" after a timeout.
"""
import faulthandler
import os
import sys

# Before gunicorn's --timeout 60, so the dump lands while the worker is alive.
STACK_DUMP_AFTER_SECONDS = float(os.environ.get('REQUEST_STACK_DUMP_SECONDS', '45'))


def post_fork(server, worker):
    # One line per worker at boot — proof in the Render logs that this file
    # was loaded and the watchdog is active.
    worker.log.info("Request stack-dump watchdog on (%.0fs) pid=%s",
                    STACK_DUMP_AFTER_SECONDS, worker.pid)


def pre_request(worker, req):
    faulthandler.dump_traceback_later(STACK_DUMP_AFTER_SECONDS, repeat=False,
                                      file=sys.stderr, exit=False)


def post_request(worker, req, environ, resp):
    faulthandler.cancel_dump_traceback_later()
