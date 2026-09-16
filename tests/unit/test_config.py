from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hermes.config import PathsPolicy, Policy, load_policy
from tests.conftest import ROOT


def test_example_config_matches_defaults() -> None:
    assert load_policy(ROOT / "config.example.yaml") == Policy()


def test_defaults_are_safe() -> None:
    policy = Policy()
    assert policy.dry_run is True
    assert policy.approval.auto_approve == [] and policy.approval.timid is True
    assert policy.listenbrainz.playlists["weekly-jams"] == "ignore"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("quality:\n  require_fromat: FLAC\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="require_fromat"):
        load_policy(cfg)


def test_playlist_mode_is_validated(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("listenbrainz:\n  playlists:\n    weekly-jams: maybe\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_policy(cfg)


def test_empty_file_is_all_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("", encoding="utf-8")
    assert load_policy(cfg) == Policy()


def test_path_mapping_defaults_match_home_cluster() -> None:
    paths = PathsPolicy()
    assert paths.to_beets("/downloads/complete/hermes/12/Album") == "/downloads/hermes/12/Album"
    assert paths.to_beets("/downloads/pending/hermes/12") == "/downloads/pending/hermes/12"


def test_deluge_layout_locations() -> None:
    per = Policy.model_validate({"deluge": {"pending_root": "/p/", "completed_root": "/c"}})
    assert per.deluge.locations(12) == ("/p/12", "/c/12")
    flat = Policy.model_validate(
        {"deluge": {"pending_root": "/p/", "completed_root": "/c", "layout": "flat"}}
    )
    assert flat.deluge.locations(12) == ("/p", "/c")
    with pytest.raises(ValueError):
        Policy.model_validate({"deluge": {"layout": "nested"}})


def test_deluge_instance_routing() -> None:
    policy = Policy.model_validate(
        {
            "deluge": {
                "instances": {
                    "a": {"url": "http://a", "indexers": ["Tracker A"]},
                    "b": {"url": "http://b", "indexers": ["Tracker B"]},
                },
                "default_instance": "a",
            }
        }
    )
    assert policy.deluge.instance_for("Tracker B") == "b"
    assert policy.deluge.instance_for("tracker b") == "b"
    assert policy.deluge.instance_for("SomeOtherTracker") == "a"
    assert Policy().deluge.instance_for("Tracker B") is None
