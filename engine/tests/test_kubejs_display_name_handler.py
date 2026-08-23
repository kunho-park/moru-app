"""KubeJS display-name extraction and lossless rewrite coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
from moru_engine.handlers.kubejs_display_name import KubeJSDisplayNameHandler
from moru_engine.placeholder import PlaceholderProtector
from moru_engine.scanner import scan_modpack


@pytest.mark.asyncio
async def test_extracts_static_and_template_display_names_only(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text(
        "event.create('one').displayName('Superior Shop')\n"
        "event.create(`orb_${orb}`).displayName(`Orb of ${orb.toUpperCase()}`)\n"
        "event.create('kept').displayName(Component.translatable('item.kept'))\n"
        "// event.create('old').displayName('Commented Name')\n"
        "/* event.create('old2').displayName('Block Comment Name') */\n",
        encoding="utf-8",
    )

    handler = KubeJSDisplayNameHandler()
    extracted = await handler.extract(script)

    assert extracted == {
        "display_name.0001": "Superior Shop",
        "display_name.0002": "Orb of ${orb.toUpperCase()}",
    }


@pytest.mark.asyncio
async def test_apply_changes_only_display_name_contents(tmp_path: Path) -> None:
    script = tmp_path / "modpack/kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    original = (
        "const untouched = 'Orb of Nothing'\n"
        "event.create('shop').displayName('Superior Shop').texture('x:y')\n"
        "event.create(`orb_${orb}`).displayName(`Orb of ${orb.toUpperCase()}`)\n"
    )
    script.write_text(original, encoding="utf-8")
    output = tmp_path / "out/kubejs/startup_scripts/items.js"

    await KubeJSDisplayNameHandler().apply(
        script,
        {
            "display_name.0001": "고급 상점",
            "display_name.0002": "${orb.toUpperCase()}의 오브",
        },
        output,
    )

    rewritten = output.read_text(encoding="utf-8")
    assert "const untouched = 'Orb of Nothing'" in rewritten
    assert ".displayName('고급 상점').texture('x:y')" in rewritten
    assert ".displayName(`${orb.toUpperCase()}의 오브`)" in rewritten


@pytest.mark.asyncio
async def test_scanner_includes_kubejs_display_name_script(tmp_path: Path) -> None:
    modpack = tmp_path / "modpack"
    script = modpack / "kubejs/startup_scripts/items.js"
    script.parent.mkdir(parents=True)
    script.write_text("event.create('x').displayName('Example Item')", encoding="utf-8")

    result = await scan_modpack(modpack)

    assert any(pair.source_path == script for pair in result.source_only_files)


def test_template_expression_round_trips_as_one_placeholder() -> None:
    protected = PlaceholderProtector().protect("Orb of ${orb.toUpperCase()}")

    assert protected.protected == "Orb of {{VAR}}"
    assert protected.restore("{{VAR}}의 오브") == "${orb.toUpperCase()}의 오브"
