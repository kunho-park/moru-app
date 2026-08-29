"""Manual (hand) translation write path.

Three contracts are load-bearing here and each has a real failure mode:

* A per-entry save must NOT rewrite the session snapshot. It used to, which
  made every edit O(entries) and, on a large pack, rewrote tens of megabytes to
  commit one string.
* An acknowledged edit must survive process death. The journal is fsync'd on
  append and replayed on load; a torn trailing line must cost only itself.
* A hand translation whose source text later changed must be reported as such.
  Entry identity is the path-derived ``(file, key)`` and nothing hashes the
  source, so without the recorded ``src_sha`` a stale translation is
  indistinguishable from a current one. "No hash recorded" is deliberately a
  third state, not a synonym for clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moru_engine.pipeline import EntryStatus
from moru_engine.server.app import create_app
from moru_engine.server.manual_journal import (
    JOURNAL_SUFFIX,
    ManualJournal,
    StaleState,
    entry_ref,
    source_sha,
)
from moru_engine.server.sessions import SessionStore

AUTH = {"authorization": "Bearer tok"}
SRC1 = "Take a pickaxe."
SRC2 = "Bring torches."


def _session_payload(pack: Path) -> dict:
    return {
        "version": "1.0",
        "id": "job1",
        "modpack_name": "Stub",
        "modpack_path": str(pack),
        "source_locale": "en_us",
        "target_locale": "ko_kr",
        "model": "",
        "status": "done",
        "created_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "stats": {},
        "config": {
            "modpack_path": str(pack),
            "source_locale": "en_us",
            "target_locale": "ko_kr",
        },
        "identity": None,
        "scan_result": None,
        "entries": [
            {
                "key": "q.desc1",
                "file": "a.snbt",
                "source_text": SRC1,
                "translated_text": None,
                "status": "failed",
                "errors": ["boom"],
            },
            {
                "key": "q.desc2",
                "file": "a.snbt",
                "source_text": SRC2,
                "translated_text": "",
                "status": "failed",
                "errors": [],
            },
        ],
        "done_payload": None,
    }


@pytest.fixture
def manual_session(tmp_path: Path):
    """A registered, finished translate session reachable over HTTP."""
    pack = tmp_path / "pack"
    pack.mkdir()
    cfg_root = tmp_path / "cfg"
    imported = tmp_path / "job1.moru"
    imported.write_text(json.dumps(_session_payload(pack)), encoding="utf-8")

    app = create_app("tok", config_dir=cfg_root)
    client = TestClient(app)
    res = client.post(
        "/sessions/import", json={"input_path": str(imported)}, headers=AUTH
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["job"]["id"]
    return {
        "client": client,
        "job_id": job_id,
        "cfg_root": cfg_root,
        "imported": imported,
        "journal": cfg_root / "sessions" / f"{job_id}{JOURNAL_SUFFIX}",
        "snapshot": cfg_root / "sessions" / f"{job_id}.moru",
    }


def _patch(ctx, key: str, **body) -> dict:
    body.setdefault("file", "a.snbt")
    res = ctx["client"].patch(
        f"/translate/{ctx['job_id']}/entries/{key}", json=body, headers=AUTH
    )
    assert res.status_code == 200, res.text
    return res.json()


def _kinds(journal: Path) -> list[str]:
    return [
        json.loads(line)["t"]
        for line in journal.read_text(encoding="utf-8").strip().splitlines()
    ]


def test_draft_is_durable_but_does_not_settle_the_entry(manual_session):
    body = _patch(
        manual_session,
        "q.desc1",
        translated_text="곡괭이를",
        commit=False,
        src_sha=source_sha(SRC1),
    )
    # An in-progress translation must never be exportable.
    assert body["translated_text"] == ""
    assert body["status"] == "failed"
    assert _kinds(manual_session["journal"]) == ["draft"]


def test_commit_settles_the_entry_and_records_human_origin(manual_session):
    body = _patch(
        manual_session,
        "q.desc1",
        translated_text="곡괭이를 챙기세요.",
        commit=True,
        origin="human",
        src_sha=source_sha(SRC1),
    )
    assert body["translated_text"] == "곡괭이를 챙기세요."
    assert body["status"] == "modified"
    assert body["origin"] == "human"
    assert body["stale_source"] is False
    assert _kinds(manual_session["journal"]) == ["commit"]


def test_per_entry_save_does_not_rewrite_the_snapshot(manual_session):
    """The regression this whole module exists to prevent."""
    snapshot = manual_session["snapshot"]
    before = snapshot.stat().st_mtime_ns
    for i in range(5):
        _patch(
            manual_session,
            "q.desc1",
            translated_text=f"버전 {i}",
            commit=True,
            src_sha=source_sha(SRC1),
        )
    assert snapshot.stat().st_mtime_ns == before
    assert len(_kinds(manual_session["journal"])) == 5


def test_commit_supersedes_an_earlier_draft(manual_session):
    _patch(manual_session, "q.desc1", translated_text="초안", commit=False)
    _patch(
        manual_session,
        "q.desc1",
        translated_text="최종",
        commit=True,
        src_sha=source_sha(SRC1),
    )
    state = ManualJournal(manual_session["journal"]).replay()
    entry = state[entry_ref("a.snbt", "q.desc1")]
    assert entry.text == "최종"
    assert entry.draft is None


def test_flag_round_trips_and_drives_its_own_bucket(manual_session):
    body = _patch(
        manual_session, "q.desc2", translated_text="", commit=False, flagged=True
    )
    assert body["flagged"] is True

    res = manual_session["client"].get(
        f"/translate/{manual_session['job_id']}/entries?filter=flagged", headers=AUTH
    )
    assert [e["key"] for e in res.json()["entries"]] == ["q.desc2"]

    body = _patch(
        manual_session, "q.desc2", translated_text="", commit=False, flagged=False
    )
    assert body["flagged"] is False


def test_counts_returns_every_bucket_in_one_call(manual_session):
    _patch(
        manual_session,
        "q.desc1",
        translated_text="번역",
        commit=True,
        src_sha=source_sha(SRC1),
    )
    _patch(manual_session, "q.desc2", translated_text="", commit=False, flagged=True)

    res = manual_session["client"].get(
        f"/translate/{manual_session['job_id']}/entries/counts", headers=AUTH
    )
    assert res.status_code == 200, res.text
    counts = res.json()
    assert set(counts) == {
        "all",
        "pending",
        "failed",
        "warning",
        "modified",
        "flagged",
        "stale_source",
    }
    assert counts["all"] == 2
    assert counts["modified"] == 1
    assert counts["flagged"] == 1
    assert counts["failed"] == 1


def test_counts_route_is_not_shadowed_by_the_entry_key_path(manual_session):
    """`{entry_key:path}` would swallow "counts" if registered first."""
    res = manual_session["client"].get(
        f"/translate/{manual_session['job_id']}/entries/counts", headers=AUTH
    )
    assert res.status_code == 200
    assert isinstance(res.json(), dict)
    assert "all" in res.json()


def test_committed_text_survives_dropping_the_job_from_memory(manual_session):
    """Reloading from disk must see journal edits, not just the snapshot.

    The snapshot is deliberately not rewritten per edit, so if the journal
    were not replayed on load an edit would silently vanish the moment the
    in-RAM job was evicted — which is exactly what a sidecar restart does.
    """
    _patch(
        manual_session,
        "q.desc1",
        translated_text="디스크에서 살아남아야 함",
        commit=True,
        src_sha=source_sha(SRC1),
    )
    manual_session["client"].app.state  # keep the app referenced

    store = SessionStore(sessions_dir=manual_session["cfg_root"] / "sessions")
    reloaded = store.load_job_session(manual_session["job_id"])
    assert reloaded is not None
    entry = next(
        e for e in reloaded.result.entries if e.key == "q.desc1" and e.file == "a.snbt"
    )
    assert entry.translated_text == "디스크에서 살아남아야 함"
    assert entry.status is EntryStatus.MODIFIED


def test_a_draft_is_not_applied_on_reload(manual_session):
    """An unfinished translation must never look settled after a restart."""
    _patch(manual_session, "q.desc1", translated_text="아직 초안", commit=False)

    store = SessionStore(sessions_dir=manual_session["cfg_root"] / "sessions")
    reloaded = store.load_job_session(manual_session["job_id"])
    assert reloaded is not None
    entry = next(
        e for e in reloaded.result.entries if e.key == "q.desc1" and e.file == "a.snbt"
    )
    assert entry.translated_text is None
    assert entry.status is not EntryStatus.MODIFIED
    # ...but the text itself is still recoverable.
    state = ManualJournal(manual_session["journal"]).replay()
    assert state[entry_ref("a.snbt", "q.desc1")].draft == "아직 초안"


def test_state_survives_a_fresh_process(manual_session):
    _patch(
        manual_session,
        "q.desc1",
        translated_text="살아남아야 함",
        commit=True,
        src_sha=source_sha(SRC1),
    )
    _patch(manual_session, "q.desc2", translated_text="", commit=False, flagged=True)

    # A new app over the same config root is what an app restart looks like.
    client = TestClient(create_app("tok", config_dir=manual_session["cfg_root"]))
    res = client.post(
        "/sessions/import",
        json={"input_path": str(manual_session["imported"])},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text

    res = client.get(
        f"/translate/{manual_session['job_id']}/entries?filter=flagged", headers=AUTH
    )
    assert [e["key"] for e in res.json()["entries"]] == ["q.desc2"]

    state = ManualJournal(manual_session["journal"]).replay()
    assert state[entry_ref("a.snbt", "q.desc1")].text == "살아남아야 함"


def test_a_torn_trailing_line_costs_only_itself(manual_session):
    _patch(
        manual_session,
        "q.desc1",
        translated_text="지켜져야 함",
        commit=True,
        src_sha=source_sha(SRC1),
    )
    with manual_session["journal"].open("a", encoding="utf-8") as fh:
        fh.write('{"t":"commit","file":"a.snbt","key":"q.desc1","tex')

    state = ManualJournal(manual_session["journal"]).replay()
    assert state[entry_ref("a.snbt", "q.desc1")].text == "지켜져야 함"


@pytest.mark.parametrize(
    ("recorded_sha", "current_source", "expected"),
    [
        (source_sha(SRC1), SRC1, StaleState.CLEAN),
        (source_sha(SRC1), "Take a NETHERITE pickaxe.", StaleState.STALE),
        (None, SRC1, StaleState.UNKNOWN),
    ],
)
def test_drift_is_three_valued(recorded_sha, current_source, expected, tmp_path):
    journal = ManualJournal(tmp_path / f"x{JOURNAL_SUFFIX}")
    journal.commit(
        file="a.snbt", key="k", text="번역", src_sha=recorded_sha, origin="human"
    )
    state = journal.replay()[entry_ref("a.snbt", "k")]
    assert state.stale_against(current_source) is expected


def test_stale_translation_is_surfaced_not_discarded(manual_session):
    """A changed source must neither drop the work nor certify it."""
    _patch(
        manual_session,
        "q.desc1",
        translated_text="사람이 쓴 번역",
        commit=True,
        src_sha=source_sha("a completely different source string"),
    )
    res = manual_session["client"].get(
        f"/translate/{manual_session['job_id']}/entries?filter=stale_source",
        headers=AUTH,
    )
    entries = res.json()["entries"]
    assert [e["key"] for e in entries] == ["q.desc1"]
    # The text is still there — staleness is a warning, not a deletion.
    assert entries[0]["translated_text"] == "사람이 쓴 번역"
    assert entries[0]["stale_source"] is True


def test_deleting_a_session_removes_its_journal(manual_session):
    _patch(manual_session, "q.desc1", translated_text="x", commit=False)
    assert manual_session["journal"].is_file()

    res = manual_session["client"].delete(
        f"/sessions/{manual_session['job_id']}", headers=AUTH
    )
    assert res.status_code == 200, res.text
    # A later session reusing the id must not inherit these edits.
    assert not manual_session["journal"].is_file()
