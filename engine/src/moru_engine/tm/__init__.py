"""Translation memory: local SQLite exact-match cache."""

from .local_tm import (
    MANUAL_ORIGIN,
    META_LAST_SHARED_VERSION,
    SHARED_GLOSSARY_VERSION,
    LocalTM,
    TMStats,
    default_db_path,
    is_cacheable_pair,
    tm_key,
)

__all__ = [
    "MANUAL_ORIGIN",
    "META_LAST_SHARED_VERSION",
    "SHARED_GLOSSARY_VERSION",
    "LocalTM",
    "TMStats",
    "default_db_path",
    "is_cacheable_pair",
    "tm_key",
]
