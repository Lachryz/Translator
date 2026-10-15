from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from paragraph_translator import (
    ParagraphTranslator,
    create_translator,
    split_paragraphs,
)
from translator_runtime import resolve_config_path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "translator_config.json"

__all__ = [
    "InvalidTranslationInput",
    "TranslationFailed",
    "TranslationResult",
    "Translator",
    "clear_translator_cache",
    "get_translator",
    "translate",
    "translate_with_metadata",
]


class InvalidTranslationInput(ValueError):
    """The caller supplied text or options that violate the public contract."""


class TranslationFailed(RuntimeError):
    """The configured translation pipeline could not produce a valid result."""


@dataclass(frozen=True)
class TranslationResult:
    translation: str
    mode: str
    paragraph_count: int
    model: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass
class _ProgressSnapshot:
    mode: str | None = None

    def update(self, percent: int, message: str, mode: str | None = None) -> None:
        del percent, message
        if mode is not None:
            self.mode = mode


class Translator:
    """Reusable in-process interface around the existing translation engine."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = resolve_config_path(config_path)
        try:
            self._engine: ParagraphTranslator = create_translator(self.config_path)
        except Exception as exc:
            raise TranslationFailed(str(exc)) from exc
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return self._engine.settings.model

    def translate(
        self,
        text: str,
        *,
        preserve_paragraphs: bool = False,
    ) -> TranslationResult:
        _validate_input(text, preserve_paragraphs)
        progress = _ProgressSnapshot()
        try:
            # One engine instance is shared for efficiency. Serialize top-level
            # calls while preserving 2batch's internal paragraph concurrency.
            with self._lock:
                translated = self._engine.translate(
                    text,
                    status=progress,
                    preserve_paragraphs=preserve_paragraphs,
                )
        except Exception as exc:
            raise TranslationFailed(str(exc)) from exc
        return TranslationResult(
            translation=translated,
            mode=progress.mode or "",
            paragraph_count=len(split_paragraphs(text)),
            model=self.model,
        )

    def translate_text(
        self,
        text: str,
        *,
        preserve_paragraphs: bool = False,
    ) -> str:
        """Return only the translated text."""
        return self.translate(
            text,
            preserve_paragraphs=preserve_paragraphs,
        ).translation


def _validate_input(text: Any, preserve_paragraphs: Any) -> None:
    if not isinstance(text, str):
        raise InvalidTranslationInput(
            f"text must be a string, got {type(text).__name__}"
        )
    if not text.strip():
        raise InvalidTranslationInput("text must contain non-whitespace content")
    if type(preserve_paragraphs) is not bool:
        raise InvalidTranslationInput(
            "preserve_paragraphs must be a boolean, "
            f"got {type(preserve_paragraphs).__name__}"
        )


@lru_cache(maxsize=4)
def _cached_translator(config_path: str) -> Translator:
    return Translator(config_path)


_cache_lock = threading.Lock()


def get_translator(config_path: str | Path | None = None) -> Translator:
    """Return a cached reusable Translator for the resolved configuration path."""
    resolved = str(resolve_config_path(config_path))
    # lru_cache alone may initialize duplicate engines on concurrent first calls.
    with _cache_lock:
        return _cached_translator(resolved)


def clear_translator_cache() -> None:
    """Discard cached engines so changed configuration is loaded on the next call."""
    with _cache_lock:
        _cached_translator.cache_clear()


def translate(
    text: str,
    *,
    preserve_paragraphs: bool = False,
    config_path: str | Path | None = None,
) -> str:
    """Translate text with a cached engine and return only the result string."""
    return translate_with_metadata(
        text,
        preserve_paragraphs=preserve_paragraphs,
        config_path=config_path,
    ).translation


def translate_with_metadata(
    text: str,
    *,
    preserve_paragraphs: bool = False,
    config_path: str | Path | None = None,
) -> TranslationResult:
    """Translate text and return its mode, paragraph count, and model metadata."""
    _validate_input(text, preserve_paragraphs)
    return get_translator(config_path).translate(
        text,
        preserve_paragraphs=preserve_paragraphs,
    )
