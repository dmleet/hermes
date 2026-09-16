import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from beets import config

from beetsplug.hermes import AgentServer, JobStore

STUB = """\
import sys
print("ARGS", " ".join(sys.argv[1:]))
sys.exit(1 if any("fail" in a for a in sys.argv[1:]) else 0)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("BEETSDIR", str(tmp_path))
    config.clear()
    config.read(user=False, defaults=True)
    stub = tmp_path / "stub_beet.py"
    stub.write_text(STUB)
    store = JobStore(tmp_path / "hermes-jobs", [sys.executable, str(stub)])
    server = AgentServer(("127.0.0.1", _free_port()), store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", store, tmp_path
    server.shutdown()
    server.server_close()


def call(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_finished(base, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, job = call(f"{base}/jobs/{job_id}")
        assert status == 200
        if job["status"] == "finished":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_healthz_shape(agent):
    base, _, _ = agent
    status, body = call(f"{base}/healthz")
    assert status == 200
    assert body["ok"] is True and body["config_ok"] is True
    assert body["config_problems"] == [] and body["worker_busy"] is False
    assert isinstance(body["beets_version"], str) and body["beets_version"]
    assert body["agent_api"] == 1 and isinstance(body["agent_version"], str)


def test_unsafe_config_rejects_import(agent):
    base, _, tmp_path = agent
    config["import"]["copy"] = False
    status, body = call(f"{base}/healthz")
    assert body["config_ok"] is False and "import.copy" in body["config_problems"][0]
    album = tmp_path / "album"
    album.mkdir()
    status, body = call(f"{base}/import", "POST", {"path": str(album), "acquisition_id": "a1"})
    assert status == 409 and body["config_problems"]


def test_successful_job_lifecycle(agent):
    base, store, tmp_path = agent
    album = tmp_path / "Artist - Album"
    album.mkdir()
    status, body = call(
        f"{base}/import",
        "POST",
        {"path": str(album), "acquisition_id": "acq-42", "search_id": "1111-2222"},
    )
    assert status == 202
    job = wait_finished(base, body["job_id"])
    assert job["exit_code"] == 0 and job["error"] is None
    assert job["started_at"] and job["finished_at"]
    args_line = next(line for line in job["log_tail"] if line.startswith("ARGS"))
    assert "import -q -I --set hermes_acquisition=acq-42 --search-id 1111-2222 -l" in args_line
    assert args_line.endswith(str(album))
    assert Path(job["log_path"]).exists()
    listed = call(f"{base}/jobs")[1]
    assert listed[0]["job_id"] == job["job_id"]
    # state survives a restart
    reloaded = JobStore(store.jobs_dir, store.beet_command)
    assert reloaded.get(job["job_id"])["exit_code"] == 0


def test_history_records_album_groups_as_beets_would(agent):
    """A verified Hermes import is added to beets' incremental history with the same path
    tuples a manual `beet import` over the parent would compute, multi-disc included."""
    from beets.importer.state import ImportState
    from beets.util import bytestring_path

    base, store, tmp_path = agent
    downloads = tmp_path / "downloads"
    single = downloads / "hermes" / "1" / "Artist - Album"
    single.mkdir(parents=True)
    (single / "01 - One.flac").write_bytes(b"")
    (single / "cover.jpg").write_bytes(b"")
    multi = downloads / "hermes" / "2" / "Artist - Box"
    for disc in ("CD1", "CD2"):
        (multi / disc).mkdir(parents=True)
        (multi / disc / "01.flac").write_bytes(b"")

    status, body = call(f"{base}/history", "POST", {"path": str(single)})
    assert status == 200 and body["recorded"] == 1
    assert body["paths"] == [[str(single)]]
    status, body = call(f"{base}/history", "POST", {"path": str(multi)})
    assert status == 200 and body["recorded"] == 1
    # beets collapses a multi-disc folder into one group: the parent and its disc directories.
    assert sorted(body["paths"][0]) == sorted([str(multi), str(multi / "CD1"), str(multi / "CD2")])

    history = ImportState(readonly=True).taghistory
    assert (bytestring_path(str(single)),) in history
    expected = tuple(bytestring_path(p) for p in body["paths"][0])
    assert expected in history
    # Idempotent, and a missing or file path is refused.
    assert call(f"{base}/history", "POST", {"path": str(single)})[1]["recorded"] == 1
    assert len(ImportState(readonly=True).taghistory) == 2
    assert call(f"{base}/history", "POST", {"path": str(single / "01 - One.flac")})[0] == 404
    assert call(f"{base}/history", "POST", {"nope": 1})[0] == 400


def test_failing_job_reports_exit_code(agent):
    base, _, tmp_path = agent
    album = tmp_path / "fail-album"
    album.mkdir()
    status, body = call(f"{base}/import", "POST", {"path": str(album), "acquisition_id": "acq-x"})
    assert status == 202
    job = wait_finished(base, body["job_id"])
    assert job["exit_code"] == 1 and job["search_id"] is None


def test_not_found_cases(agent):
    base, _, tmp_path = agent
    assert call(f"{base}/jobs/nope")[0] == 404
    status, body = call(
        f"{base}/import", "POST", {"path": str(tmp_path / "missing"), "acquisition_id": "a"}
    )
    assert status == 404 and "does not exist" in body["error"]
    assert call(f"{base}/import", "POST", {"acquisition_id": "a"})[0] == 400


def test_console_script_reaches_the_subcommand(tmp_path):
    """`hermes-agent --help` must resolve to the plugin's subcommand via the entry point."""
    import os
    import subprocess

    (tmp_path / "config.yaml").write_text("plugins: hermes\n")
    env = {**os.environ, "BEETSDIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", "from beetsplug.hermes import main; main(['--help'])"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--port" in result.stdout and "hermes-agent" in result.stdout


def test_delete_flag_and_unreadable_flag_are_problems(agent):
    base, _, _ = agent
    config["import"]["delete"] = True
    _, body = call(f"{base}/healthz")
    assert any("import.delete" in p for p in body["config_problems"])
    config["import"]["delete"] = False
    config["import"]["copy"] = "definitely"
    _, body = call(f"{base}/healthz")
    assert body["config_ok"] is False and any("unreadable" in p for p in body["config_problems"])


def test_command_terminates_options_and_import_log_is_exposed(agent):
    base, store, tmp_path = agent
    album = tmp_path / "-leading-dash"
    album.mkdir()
    status, body = call(f"{base}/import", "POST", {"path": str(album), "acquisition_id": "a9"})
    assert status == 202
    job = wait_finished(base, body["job_id"])
    args_line = next(line for line in job["log_tail"] if line.startswith("ARGS"))
    assert " -- " in args_line and args_line.endswith(str(album))
    assert "import_log_tail" in job
    # Every Hermes import runs with the overlay that clears beets' candidate preferences:
    # the release id is given, so they could only lower the one candidate's score.
    assert f" -c {store.overlay} import " in args_line
    assert "preferred:" in store.overlay.read_text() and "media: []" in store.overlay.read_text()


def test_corrupt_jobs_file_is_moved_aside(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "jobs.json").write_text("{not json")
    store = JobStore(jobs_dir, [sys.executable, "-c", "pass"])
    assert store.list() == []
    assert (jobs_dir / "jobs.corrupt").exists()
