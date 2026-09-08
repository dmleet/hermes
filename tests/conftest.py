from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from hermes.config import Policy, Settings
from hermes.db import make_engine, make_session_factory
from hermes.domain.models import Base

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        hermes_config_path=ROOT / "config.example.yaml",
        hermes_data_dir=tmp_path,
        hermes_database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        prowlarr_url="http://prowlarr.test",
        prowlarr_api_key="key",
        deluge_password="pw",
        musicbrainz_contact="test@example.com",
    )


@pytest.fixture
def policy() -> Policy:
    return Policy.model_validate(
        {
            "beets": {"agent_url": "http://beets.test:8338"},
            "quality": {"max_sample_rate_khz": None},
            "deluge": {
                "instances": {
                    "b": {"url": "http://deluge-b.test", "indexers": ["Tracker B"]},
                    "a": {"url": "http://deluge-a.test", "indexers": ["Tracker A"]},
                }
            },
        }
    )


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
    engine.dispose()
