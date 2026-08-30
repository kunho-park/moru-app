"""Pydantic models for translation pipeline."""

from .callbacks import ScanProgressCallback
from .glossary import (
    FormattingRule,
    Glossary,
    ProperNounRule,
    TermRule,
    is_key_scope_pattern,
    key_scope_covers,
)
from .translation import LanguageFilePair
from .validation import (
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
    ValidationType,
)

__all__ = [
    # Callbacks
    "ScanProgressCallback",
    # Glossary
    "FormattingRule",
    "Glossary",
    "ProperNounRule",
    "TermRule",
    "is_key_scope_pattern",
    "key_scope_covers",
    # Translation
    "LanguageFilePair",
    # Validation
    "ValidationIssue",
    "ValidationResult",
    "ValidationSeverity",
    "ValidationType",
]
