"""Keep pure offline tests runnable without the optional private backend."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest

from translator_runtime import resolve_runtime_paths


def _two_batch_available() -> bool:
    backend = resolve_runtime_paths().two_batch_root
    if not backend.is_dir():
        return False
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    if importlib.util.find_spec("tobatch") is None:
        return False
    try:
        importlib.import_module("tobatch.runner")
        importlib.import_module("tobatch.validation")
    except ImportError:
        return False
    return True


requires_two_batch = unittest.skipUnless(
    _two_batch_available(), "optional local 2batch backend or its dependencies are unavailable",
)
