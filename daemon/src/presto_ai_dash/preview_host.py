"""The Presto's screen in a browser, served by the daemon when asked.

Off unless ``DASH_PRESTO_PREVIEW`` is on. It is a test aid: it runs the real
Presto app (the files baked into the container image under
``/opt/presto-preview``) on an emulated 240x240 display, polls this daemon as a
device named ``psl``, and shows the result at one address, so a container can be
tried without a physical Presto. The page needs no sign-in and shows live
account names and quotas, so it listens on loopback unless
``DASH_PRESTO_PREVIEW_HOST`` says otherwise; publish its port on the host's
loopback address only.
"""

import logging
import os
import sys
import threading
import types

from .config import ConfigError, parse_flag

PREVIEW_ENV = "DASH_PRESTO_PREVIEW"
HOST_ENV = "DASH_PRESTO_PREVIEW_HOST"
PORT_ENV = "DASH_PRESTO_PREVIEW_PORT"
ALLOWED_HOSTS_ENV = "DASH_PRESTO_PREVIEW_ALLOWED_HOSTS"
DEFAULT_PORT = 8790
# Where the image keeps the app and the preview tool: the same layout as the repository.
PREVIEW_ROOT = "/opt/presto-preview"

log = logging.getLogger(__name__)


def preview_settings(environ=None):
    """(host, port, names) from the environment, or None when the preview is off.

    Raises ConfigError for a value that cannot be used.
    """
    environ = os.environ if environ is None else environ
    if PREVIEW_ENV not in environ or not parse_flag(environ[PREVIEW_ENV], PREVIEW_ENV):
        return None
    host = (environ.get(HOST_ENV) or "127.0.0.1").strip()
    try:
        port = int(environ.get(PORT_ENV) or DEFAULT_PORT)
    except ValueError:
        raise ConfigError("%s must be a port number" % PORT_ENV) from None
    if not 1 <= port <= 65535:
        raise ConfigError("%s must be a port number" % PORT_ENV)
    listed = [name.strip().lower() for name in (environ.get(ALLOWED_HOSTS_ENV) or "").split(",")]
    names = [name for name in listed if name] or ["127.0.0.1:%d" % port, "localhost:%d" % port]
    if any(not name.isascii() or "/" in name or len(name) > 255 for name in names):
        raise ConfigError("%s entries are host:port names" % ALLOWED_HOSTS_ENV)
    return host, port, names


class RunningPreview(object):
    """One running preview: the app thread and the page's server."""

    def __init__(self, runner, server):
        self.runner = runner
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, args=(0.02,), name="presto-preview",
                                       daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self.runner.stop()


def start_preview(daemon_port, shared_token, root=PREVIEW_ROOT, environ=None):
    """Start the preview when it is switched on, and return it, or None.

    A test aid must never stop the daemon: every failure is logged and the
    preview stays off.
    """
    try:
        settings = preview_settings(environ)
    except ConfigError as exc:
        log.error("the Presto preview stays off: %s", exc)
        return None
    if settings is None:
        return None
    host, port, names = settings
    tools = os.path.join(root, "tools", "presto_preview")
    if not os.path.isfile(os.path.join(tools, "live.py")):
        log.error("the Presto preview stays off: its files are not at %s "
                  "(they ship in the container image)", root)
        return None
    for folder in (os.path.join(root, "tools"), tools):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    # The app reads its daemon address and token from a module named config. The
    # preview builds it in memory from this daemon's own settings, so no device
    # file and no token is written anywhere.
    settings_module = types.ModuleType("config")
    settings_module.DAEMON_HOST = "127.0.0.1"
    settings_module.DAEMON_PORT = int(daemon_port)
    settings_module.SHARED_TOKEN = shared_token
    sys.modules["config"] = settings_module
    try:
        import live
        runner = live.Runner(demo=False)
        runner.restart()
        try:
            server = live.start_server(runner, host, port, names)
        except OSError as exc:
            runner.stop()
            log.error("the Presto preview stays off: cannot listen on %s:%d (%s)",
                      host, port, type(exc).__name__)
            return None
        preview = RunningPreview(runner, server).start()
    except Exception as exc:  # a test aid: never take the daemon down
        log.error("the Presto preview stays off: %s", type(exc).__name__)
        return None
    log.info("Presto preview: http://%s:%d/ (a test aid with no sign-in; keep its port private)",
             host, port)
    return preview
