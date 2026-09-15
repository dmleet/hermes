from __future__ import annotations

from hermes.domain.models import Candidate


def test_info_link_only_follows_a_real_url() -> None:
    """The title is a link only when the indexer gave a page to open. Prowlarr reports
    `infoUrl` per indexer: some give none, and a value that is not an http(s) URL would
    render a dead or surprising link."""
    assert Candidate(info_url="https://tracker.example/torrents.php?id=1").info_link
    assert Candidate(info_url="http://tracker.example/t/1").info_link
    assert Candidate(info_url=None).info_link is None
    assert Candidate(info_url="").info_link is None
    assert Candidate(info_url="magnet:?xt=urn:btih:deadbeef").info_link is None
    assert Candidate(info_url="javascript:alert(1)").info_link is None
