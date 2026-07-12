"""Pydantic models for translation file pairs."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class LanguageFilePair(BaseModel):
    """A pair of source and target language files."""

    source_path: Path = Field(
        ...,
        description="Path to source language file (e.g., en_us.json)",
    )
    target_path: Path | None = Field(
        default=None,
        description="Path to existing target language file (e.g., ko_kr.json)",
    )
    namespace: str = Field(
        default="",
        description="Mod namespace (e.g., 'minecraft', 'mekanism')",
    )
    mod_id: str = Field(
        default="",
        description="Mod identifier",
    )

    @property
    def has_existing_translation(self) -> bool:
        """Check if a target translation already exists."""
        return self.target_path is not None and self.target_path.exists()
