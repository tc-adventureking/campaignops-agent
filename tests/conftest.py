from pathlib import Path

import pytest

from app.settings import Settings
from scripts.seed_data import seed_data


@pytest.fixture(scope="session")
def seeded_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("synthetic")
    seed_data(directory)
    return directory


@pytest.fixture
def settings(seeded_dir: Path, tmp_path: Path) -> Settings:
    # Explicit demo and empty key: tests never inherit a paid API mode or key from .env.
    settings = Settings(_env_file=None, agent_mode="demo", model_api_key="", data_dir=seeded_dir)  # type: ignore[call-arg]
    return settings
