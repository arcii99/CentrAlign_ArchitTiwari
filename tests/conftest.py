"""Run tests against an isolated world (own directory and ports) so the dev world is never touched."""

import os
import socket
import tempfile
import threading
import time

import pytest

os.environ["NW_WORLD_DIR"] = tempfile.mkdtemp(prefix="nw_world_test_")
# Overridable so two test sessions (e.g. parallel development) can run side by side.
os.environ.setdefault("NW_PORTAL_PORT", "18101")
os.environ.setdefault("NW_MAIL_PORT", "18102")


def _wait_for(port: int, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"server on port {port} did not start")


@pytest.fixture(scope="session")
def servers():
    import uvicorn

    from world import config
    from world.scenarios import reset

    reset("base")  # the apps need a world on disk before the first request
    servs = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
             for app, port in (("world.portal.app:app", config.PORTAL_PORT), ("world.mail.app:app", config.MAIL_PORT))]
    for s in servs:
        threading.Thread(target=s.run, daemon=True).start()
    for port in (config.PORTAL_PORT, config.MAIL_PORT):
        _wait_for(port)
    yield config
    for s in servs:
        s.should_exit = True


@pytest.fixture
def browser_page(servers):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()
