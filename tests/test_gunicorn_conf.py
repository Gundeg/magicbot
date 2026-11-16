"""gunicorn.conf.py arms a faulthandler watchdog per request, so a request
stuck past the dump threshold writes every thread's stack to the logs —
the diagnostic for the intermittent 60 s WORKER TIMEOUTs (2026-11-16)."""
import importlib.util
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / 'gunicorn.conf.py'


def _load():
    spec = importlib.util.spec_from_file_location('gunicorn_conf_under_test', CONF)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_request_hooks_arm_and_cancel_the_stack_dump(monkeypatch):
    import faulthandler
    calls = []
    monkeypatch.setattr(faulthandler, 'dump_traceback_later',
                        lambda timeout, **kw: calls.append(('arm', timeout, kw.get('exit'))))
    monkeypatch.setattr(faulthandler, 'cancel_dump_traceback_later',
                        lambda: calls.append(('cancel',)))
    conf = _load()
    conf.pre_request(None, None)
    conf.post_request(None, None, {}, None)
    assert calls == [('arm', 45.0, False), ('cancel',)]


def test_dump_fires_before_gunicorns_timeout():
    """The dump must land BEFORE gunicorn kills the worker at --timeout 60."""
    conf = _load()
    assert conf.STACK_DUMP_AFTER_SECONDS < 60


def test_post_fork_logs_that_the_watchdog_is_on():
    logged = []

    class _Log:
        def info(self, msg, *args):
            logged.append(msg % args)

    class _Worker:
        pid = 4242
        log = _Log()
    _load().post_fork(None, _Worker())
    assert logged == ['Request stack-dump watchdog on (45s) pid=4242']
