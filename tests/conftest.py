"""Shared fixtures for ir-search parser/contract tests.

No network: every test monkeypatches the fetcher (or calls parsers directly
on local HTML fixtures). Scripts are loaded by path via importlib so the
tests exercise exactly what ships in scripts/.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "ir-search" / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_script(name):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))  # for `from run_manifest import ...`
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def sources_crawl():
    return load_script("sources_crawl")


@pytest.fixture(scope="session")
def kstartup_crawl():
    return load_script("kstartup_crawl")


@pytest.fixture(scope="session")
def kstartup_api():
    # Return the SAME module instance kstartup_crawl imports (real import, not
    # load_script) so autouse/monkeypatch on load_key reaches the crawler.
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import kstartup_api as mod
    return mod


@pytest.fixture(scope="session")
def diff_surveys():
    return load_script("diff_surveys")


@pytest.fixture(scope="session")
def run_manifest():
    return load_script("run_manifest")


@pytest.fixture(scope="session")
def attach_download():
    # 크롤러들이 `import attach_download`로 참조하는 것과 **같은 모듈 인스턴스**를
    # 돌려준다 — load_script로 새로 만들면 monkeypatch가 크롤러 쪽에 안 먹는다.
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import attach_download as mod
    return mod


@pytest.fixture(scope="session")
def fixture_html():
    def read(name):
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")
    return read


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Politeness delays are pointless against local fixtures."""
    import time
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_key_loader: opt out of the hermetic no-key default to test load_key itself",
    )


@pytest.fixture(autouse=True)
def _hermetic_no_data_go_kr_key(monkeypatch, request):
    """Default: cmd_list sees NO data.go.kr key, so the existing crawl-path
    tests are unaffected by a real `.env` on the developer machine. Tests that
    exercise the real key loader opt out with @pytest.mark.real_key_loader.
    """
    if request.node.get_closest_marker("real_key_loader"):
        return
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import kstartup_api  # same module object kstartup_crawl imports
    except Exception:  # noqa: BLE001 — module may not be importable in some runs
        return
    monkeypatch.setattr(kstartup_api, "load_key", lambda: None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard guard: any real socket connection fails the test."""
    import socket

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
