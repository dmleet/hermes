from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings

pytestmark = pytest.mark.respx(assert_all_called=False)


def _deluge_router(router: respx.Router, host: str, *, plugins: list[str]) -> None:
    def rpc(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        results = {
            "auth.login": True,
            "web.connected": True,
            "daemon.get_version": "2.2.0",
            "core.get_enabled_plugins": plugins,
        }
        return httpx.Response(
            200, json={"id": body["id"], "result": results[body["method"]], "error": None}
        )

    router.post(f"http://{host}/json").mock(side_effect=rpc)


def _all_healthy(router: respx.Router) -> None:
    router.get("http://prowlarr.test/api/v1/system/status").respond(json={"version": "1.30.0"})
    _deluge_router(router, "deluge-b.test", plugins=["ltConfig", "Label"])
    _deluge_router(router, "deluge-a.test", plugins=["ltConfig", "Label"])
    router.get("http://beets.test:8338/healthz").respond(
        json={
            "ok": True,
            "beets_version": "2.13.1",
            "agent_api": 2,
            "agent_version": "0.5.0",
            "config_ok": True,
            "config_problems": [],
        }
    )


@pytest.fixture
def client(settings: Settings, policy: Policy) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        policy=policy,
        clients=Clients.from_config(settings, policy),
        scheduler=False,
    )
    with TestClient(app) as c:
        yield c


def test_healthz_all_green(respx_mock: respx.Router, client: TestClient) -> None:
    _all_healthy(respx_mock)
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert set(body["checks"]) == {
        "db",
        "prowlarr",
        "deluge:b",
        "deluge:a",
        "beets",
        "musicbrainz",
        "navidrome",
        "listenbrainz",
    }
    assert body["checks"]["musicbrainz"]["user_agent"].startswith("hermes/")
    assert body["checks"]["deluge:b"]["version"] == "2.2.0"
    assert body["checks"]["beets"]["beets_version"] == "2.13.1"


def test_healthz_reports_beets_config_problem(respx_mock: respx.Router, client: TestClient) -> None:
    _all_healthy(respx_mock)
    respx_mock.get("http://beets.test:8338/healthz").respond(
        json={
            "ok": True,
            "beets_version": "2.13.1",
            "agent_api": 2,
            "config_ok": False,
            "config_problems": ["import.copy is false"],
        }
    )
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["beets"]["detail"] == "import.copy is false"


def test_healthz_requires_deluge_label_plugin(respx_mock: respx.Router, client: TestClient) -> None:
    _all_healthy(respx_mock)
    _deluge_router(respx_mock, "deluge-a.test", plugins=["ltConfig"])
    resp = client.get("/healthz")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert "Label" in checks["deluge:a"]["detail"]
    assert checks["deluge:b"]["ok"] is True


def test_healthz_unreachable_service(respx_mock: respx.Router, client: TestClient) -> None:
    _all_healthy(respx_mock)
    respx_mock.get("http://prowlarr.test/api/v1/system/status").mock(
        side_effect=httpx.ConnectError("refused")
    )
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["prowlarr"]["ok"] is False


def test_unconfigured_optional_services_are_skipped(
    respx_mock: respx.Router, tmp_path: Any, policy: Policy
) -> None:
    _all_healthy(respx_mock)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        hermes_data_dir=tmp_path,
        hermes_database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        musicbrainz_contact="test@example.com",
    )
    app = create_app(settings=settings, policy=policy, scheduler=False)
    with TestClient(app) as c:
        body = c.get("/healthz").json()
    assert body["checks"]["prowlarr"] == {
        "ok": True,
        "configured": False,
        "detail": "not configured",
    }
    # Instances are configured in the policy but no password was given: that is a
    # misconfiguration, not "not configured", and the health page must say so.
    assert body["ok"] is False
    assert "DELUGE_PASSWORD" in body["checks"]["deluge"]["detail"]


def test_agent_api_mismatch_is_unhealthy(respx_mock: respx.Router, client: TestClient) -> None:
    """An older beets-hermes image paired with a newer Hermes: red, with the fix named."""
    _all_healthy(respx_mock)
    respx_mock.get("http://beets.test:8338/healthz").respond(
        json={"ok": True, "beets_version": "2.5.1", "config_ok": True, "config_problems": []}
    )
    resp = client.get("/healthz")
    assert resp.status_code == 503
    beets = resp.json()["checks"]["beets"]
    assert beets["ok"] is False and "agent API unknown" in beets["detail"]
    assert "deploy the beets-hermes image" in beets["detail"]


def test_livez_answers_without_calling_any_dependency(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """The probe route: every dependency could be down and the UI stays in the Service."""
    resp = client.get("/livez")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert not respx_mock.calls  # no network call at all


async def test_a_job_failing_for_two_intervals_shows_in_healthz_and_on_pages(
    respx_mock: respx.Router, client: TestClient
) -> None:
    from datetime import UTC, datetime, timedelta

    from alembic import command

    from hermes.app import _job
    from hermes.cli import alembic_config
    from hermes.job_status import JobStatus

    command.upgrade(alembic_config(client.app.state.settings), "head")  # the queue page
    _all_healthy(respx_mock)
    app = client.app
    status = JobStatus(every=timedelta(seconds=45))
    app.state.job_status["import"] = status

    async def broken(session, ctx):
        raise RuntimeError("database is locked")

    async def fine(session, ctx):
        return {}

    await _job(app, "import", broken)()
    assert status.failing_since is not None and "database is locked" in status.last_error
    # One bad tick is noise: still green.
    assert client.get("/healthz").json()["checks"]["jobs"]["ok"] is True
    status.failing_since = datetime.now(UTC) - timedelta(minutes=5)
    body = client.get("/healthz").json()
    assert body["ok"] is False and body["checks"]["jobs"]["ok"] is False
    assert "import job has been failing" in body["checks"]["jobs"]["detail"]
    page = client.get("/queue", headers={"accept": "text/html"}).text
    assert "import job has been failing" in page and "database is locked" in page

    await _job(app, "import", fine)()
    assert status.failing_since is None and status.last_ok is not None
    assert client.get("/healthz").json()["checks"]["jobs"]["ok"] is True
    assert "has been failing" not in client.get("/queue", headers={"accept": "text/html"}).text
