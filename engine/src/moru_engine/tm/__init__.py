"""Translation memory: local SQLite exact-match cache."""

from .local_tm import (
    META_LAST_SHARED_VERSION,
    SHARED_GLOSSARY_VERSION,
    LocalTM,
    TMStats,
    default_db_path,
    tm_key,
)

__all__ = [
    "META_LAST_SHARED_VERSION",
    "SHARED_GLOSSARY_VERSION",
    "LocalTM",
    "TMStats",
    "default_db_path",
    "tm_key",
]
