"""Where the editable config files (costs.yaml, models.yaml) live.

In the repo they sit next to the `app` package; in the Docker image the package is installed
into site-packages while the files are copied to /srv, so an explicit directory wins."""

import os
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def config_file(name: str) -> Path:
    env = os.environ.get("CARSCAN_CONFIG_DIR")
    candidates = [Path(env) / name] if env else []
    candidates += [Path.cwd() / name, _PACKAGE_ROOT / name]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]
