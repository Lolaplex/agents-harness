"""Suite-wide env scrub.

``runner.loop.main()`` pins ``AGENTS_*`` vars in-process (correct at CLI
entry, but tests invoke it in-process). Snapshot every ``AGENTS_*`` var
around each test so nothing leaks between tests.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _agents_env_snapshot():
    saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTS_")}
    yield
    for key in [k for k in os.environ if k.startswith("AGENTS_")]:
        os.environ.pop(key, None)
    os.environ.update(saved)
