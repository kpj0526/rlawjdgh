import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chrome_jumper.config import DEFAULT_JUMPS  # noqa: E402
from chrome_jumper.runner import RunOptions  # noqa: E402
from mock_site.server import serve  # noqa: E402

# 테스트는 기본으로 Chrome 창을 숨긴다. 화면으로 보려면 CJ_TEST_HEADED=1
HEADLESS = os.environ.get("CJ_TEST_HEADED") != "1"


@pytest.fixture(scope="session")
def mock():
    httpd, state = serve(0, cooldown=600, slow=30)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{httpd.server_port}/owner", "state": state, "port": httpd.server_port}
    httpd.shutdown()


@pytest.fixture
def fresh(mock):
    mock["state"].reset()
    mock["state"].cooldown = 600
    return mock


@pytest.fixture
def opts(fresh):
    return RunOptions(url=fresh["url"], jump_labels=list(DEFAULT_JUMPS), headless=HEADLESS,
                      step_timeout_sec=6, dialog_timeout_sec=3, verify_timeout_sec=3)
