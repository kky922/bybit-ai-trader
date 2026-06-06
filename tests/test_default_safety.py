import os
import subprocess
import sys


def test_default_configuration_starts_in_dry_run():
    env = os.environ.copy()
    env.pop("DRY_RUN", None)
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.DRY_RUN)"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "True"
