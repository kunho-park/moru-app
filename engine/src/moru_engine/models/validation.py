"""Pydantic models for translation validation."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ValidationSeverity(str, Enum):
    """Severity level of a validation issue."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationType(str, Enum):
    """Type of validation check."""

    PLACEHOLDER_COUNT = "placeholder_count"
    PLACEHOLDER_ORDER = "placeholder_order"
    COLOR_CODE = "color_code"
    KEY_MISMATCH = "key_mismatch"
    EMPTY_TRANSLATION = "empty_translation"
    UNTRANSLATED = "untranslated"
    FORMAT_STRING = "format_string"
    LENGTH_RATIO = "length_ratio"
    GLOSSARY_TERM_MISMATCH = "glossary_term_mismatch"
    GLOSSARY_NOUN_MISMATCH = "glossary_noun_mismatch"


class ValidationIssue(BaseModel):
    """A single validation issue found in a translation."""

    issue_type: ValidationType = Field(
        ...,
        description="Type of validation issue",
    )
    severity: ValidationSeverity = Field(
        default=ValidationSeverity.ERROR,
        description="Severity of the issue",
    )
    key: str = Field(
        ...,
        description="Translation key with the issue",
    )
    message: str = Field(
        ...,
        description="Human-readable description of the issue",
    )
    source_value: str | None = Field(
        default=None,
        description="Original source value",
    )
    translated_value: str | None = Field(
        default=None,
        description="Translated value with issue",
    )
    suggestion: str | None = Field(
        default=None,
        description="Suggested fix for the issue",
    )


class ValidationResult(BaseModel):
    """Result of validating a translation."""

    is_valid: bool = Field(
        ...,
        description="Whether the translation passed validation",
    )
    issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="List of validation issues found",
    )
    errors_count: int = Field(
        default=0,
        description="Number of error-level issues",
    )
    warnings_count: int = Field(
        default=0,
        description="Number of warning-level issues",
    )

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> ValidationResult:
        """Create a validation result from a list of issues.

        Args:
            issues: List of validation issues.

        Returns:
            Validation result.
        """
        errors = sum(1 for i in issues if i.severity == ValidationSeverity.ERROR)
        warnings = sum(1 for i in issues if i.severity == ValidationSeverity.WARNING)

        return cls(
            is_valid=errors == 0,
            issues=issues,
            errors_count=errors,
            warnings_count=warnings,
        )

    def get_errors(self) -> list[ValidationIssue]:
        """Get only error-level issues."""
        return [i for i in self.issues if i.severity == ValidationSeverity.ERROR]

    def get_warnings(self) -> list[ValidationIssue]:
        """Get only warning-level issues."""
        return [i for i in self.issues if i.severity == ValidationSeverity.WARNING]
