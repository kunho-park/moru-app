"""Per-entry translation aids, and the promise that they need no provider.

The whole point of these endpoints is that a hand translator gets real help
with nothing configured: no API key, no model, no network. So the tests assert
both the content (siblings in ordinal order, glossary scoped to the entry's own
lang key, placeholders, cross-file consistency) and the absence of any
provider dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moru_engine.server.app import create_app

AUTH = {"authorization": "Bearer tok"}

QUEST = "config/ftbquests/chapters/intro.snbt"
MEK = "mods/mekanism.jar/assets/mekanism/lang/en_us.json"

SRC_DESC1 = "Take a §6diamond pickaxe§r and at least %s torches."
SRC_SHARED = "Osmium Ingot"


def _entry(key: str, file: str, source: str, translated: str | None, status: str):
    return {
        "key": key,
        "file": file,
        "source_text": source,
        "translated_text": translated,
        "status": status,
        "errors": [],
    }


def _payload(pack: Path) -> dict:
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
            "use_tm": False,
        },
        "identity": None,
        "scan_result": None,
        # desc2/desc3/desc1 deliberately out of ordinal order.
        # Untranslated entries use "failed": EntryStatus.PENDING is part of
        # the blocked manual-seed change in orchestrator.py, so it cannot be
        # used here yet. Nothing in these tests depends on which status an
        # untranslated entry carries, only that translated_text is None.
        "entries": [
            _entry("quest.1.desc2", QUEST, "It will not survive the heat.", None, "failed"),
            _entry("quest.1.desc3", QUEST, "Netherite is the reward.", None, "failed"),
            _entry("quest.1.desc1", QUEST, SRC_DESC1, None, "failed"),
            _entry("quest.1.title", QUEST, "Into the Nether", None, "failed"),
            _entry("item.mekanism.ingot_osmium", MEK, SRC_SHARED, "오스미움 주괴", "passed"),
            _entry("item.other.ingot_osmium", MEK, SRC_SHARED, "오스뮴 주괴", "passed"),
        ],
        "done_payload": None,
    }


@pytest.fixture
def session(tmp_path: Path):
    pack = tmp_path / "pack"
    pack.mkdir()
    cfg_root = tmp_path / "cfg"
    imported = tmp_path / "job1.moru"
    imported.write_text(json.dumps(_payload(pack)), encoding="utf-8")

    app = create_app("tok", config_dir=cfg_root)
    client = TestClient(app)
    res = client.post(
        "/sessions/import", json={"input_path": str(imported)}, headers=AUTH
    )
    assert res.status_code == 200, res.text
    return {
        "client": client,
        "job_id": res.json()["job"]["id"],
        "cfg_root": cfg_root,
    }


def _context(session, key: str, file: str) -> dict:
    res = session["client"].get(
        f"/translate/{session['job_id']}/entries/{key}/context?file={file}",
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    return res.json()


def _put_glossary(session, terms: list[dict]) -> None:
    res = session["client"].put(
        "/glossary",
        json={"source_lang": "en_us", "target_lang": "ko_kr", "terms": terms},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text


# ---- siblings -----------------------------------------------------------


def test_siblings_are_the_numbered_run_in_ordinal_order(session):
    ctx = _context(session, "quest.1.desc1", QUEST)
    # desc1's run is desc1..desc3; itself excluded, ordinal order regardless of
    # the order the entries were stored in.
    assert [s["key"] for s in ctx["siblings"]] == ["quest.1.desc2", "quest.1.desc3"]


def test_a_key_with_no_run_has_no_siblings(session):
    assert _context(session, "quest.1.title", QUEST)["siblings"] == []


def test_siblings_never_cross_a_file(session):
    ctx = _context(session, "item.mekanism.ingot_osmium", MEK)
    assert all(not s["key"].startswith("quest.") for s in ctx["siblings"])


# ---- placeholders -------------------------------------------------------


def test_placeholders_come_from_the_engine_protector(session):
    ctx = _context(session, "quest.1.desc1", QUEST)
    literals = [p["literal"] for p in ctx["placeholders"]]
    assert "%s" in literals
    assert "§6" in literals
    assert "§r" in literals
    # The semantic kind, not the PATTERNS key.
    assert {p["kind"] for p in ctx["placeholders"]} <= {
        "ARG",
        "VAR",
        "TAG",
        "BR",
        "COLOR",
        "RESET",
    }


# ---- cross-file consistency --------------------------------------------


def test_same_source_elsewhere_surfaces_a_disagreement(session):
    """Two mods, one English string, two different Korean strings."""
    ctx = _context(session, "item.mekanism.ingot_osmium", MEK)
    others = ctx["tm"]["same_source_elsewhere"]
    assert [o["key"] for o in others] == ["item.other.ingot_osmium"]
    assert others[0]["translated_text"] == "오스뮴 주괴"
    # This entry says 오스미움, the other says 오스뮴 — a real inconsistency.
    assert others[0]["agrees"] is False


def test_same_source_ignores_untranslated_entries(session):
    ctx = _context(session, "quest.1.desc1", QUEST)
    assert ctx["tm"]["same_source_elsewhere"] == []


# ---- glossary scoping ---------------------------------------------------


def test_glossary_returns_only_terms_matching_this_source(session):
    _put_glossary(
        session,
        [
            {"source": "Netherite", "target": "네더라이트", "origin": "manual"},
            {"source": "Osmium", "target": "오스뮴", "origin": "manual"},
        ],
    )
    ctx = _context(session, "quest.1.desc3", QUEST)
    aliases = [a for t in ctx["glossary"]["terms"] for a in t["aliases"]]
    assert "Netherite" in aliases
    # "Osmium" does not occur in this entry's source.
    assert "Osmium" not in aliases


def test_glossary_respects_key_scope(session):
    """A scoped rule must not fire on a key outside its scope."""
    _put_glossary(
        session,
        [
            {
                "source": "Netherite",
                "target": "시듦",
                "origin": "manual",
                "key_scope": ["effect.*"],
            }
        ],
    )
    ctx = _context(session, "quest.1.desc3", QUEST)
    aliases = [a for t in ctx["glossary"]["terms"] for a in t["aliases"]]
    assert "Netherite" not in aliases, (
        "a rule scoped to effect.* must not apply to a quest key"
    )


def test_glossary_scope_is_surfaced_so_the_ui_can_explain_it(session):
    _put_glossary(
        session,
        [
            {
                "source": "Netherite",
                "target": "네더라이트",
                "origin": "manual",
                "key_scope": ["quest.*"],
            }
        ],
    )
    ctx = _context(session, "quest.1.desc3", QUEST)
    terms = ctx["glossary"]["terms"]
    assert terms and terms[0]["key_scope"] == ["quest.*"]


# ---- validation ---------------------------------------------------------


def _validate(session, key: str, file: str, text: str) -> list[dict]:
    res = session["client"].post(
        f"/translate/{session['job_id']}/validate",
        json={"key": key, "file": file, "translated_text": text},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    return res.json()["issues"]


def test_validate_returns_structured_issues_not_flat_messages(session):
    issues = _validate(session, "quest.1.desc1", QUEST, "곡괭이를 챙기세요.")
    assert issues, "dropping every placeholder must produce an issue"
    first = issues[0]
    # issue_type is the stable code a client localizes on; message is English.
    assert set(first) >= {"issue_type", "severity", "message"}
    assert any(i["issue_type"] == "placeholder_count" for i in issues)
    assert any(i["severity"] == "error" for i in issues)


def test_validate_accepts_a_faithful_translation(session):
    issues = _validate(
        session,
        "quest.1.desc1",
        QUEST,
        "§6다이아몬드 곡괭이§r와 횃불 %s개를 챙기세요.",
    )
    assert [i for i in issues if i["severity"] == "error"] == []


def test_validate_reports_an_empty_translation(session):
    issues = _validate(session, "quest.1.desc1", QUEST, "")
    assert any(i["issue_type"] == "empty_translation" for i in issues)


def test_validate_404s_on_an_unknown_entry(session):
    res = session["client"].post(
        f"/translate/{session['job_id']}/validate",
        json={"key": "nope", "translated_text": "x"},
        headers=AUTH,
    )
    assert res.status_code == 404


# ---- the zero-provider promise -----------------------------------------


def test_every_aid_works_with_no_provider_configured(session, monkeypatch):
    """No key in the environment, no model in the config, no network."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    ctx = _context(session, "quest.1.desc1", QUEST)
    assert ctx["siblings"] and ctx["placeholders"]
    assert "terms" in ctx["glossary"]
    assert "same_source_elsewhere" in ctx["tm"]

    assert _validate(session, "quest.1.desc1", QUEST, "무언가") is not None

    res = session["client"].get("/placeholder/patterns", headers=AUTH)
    assert res.status_code == 200
    assert res.json()["patterns"]


def test_placeholder_patterns_are_ordered_and_compilable(session):
    import re

    res = session["client"].get("/placeholder/patterns", headers=AUTH)
    patterns = res.json()["patterns"]
    # Order is the overlap priority; patchouli macros must stay first.
    assert patterns[0]["name"] == "patchouli_macro"
    for p in patterns:
        re.compile(p["regex"])


def test_context_404s_on_an_unknown_entry(session):
    res = session["client"].get(
        f"/translate/{session['job_id']}/entries/nope/context", headers=AUTH
    )
    assert res.status_code == 404
