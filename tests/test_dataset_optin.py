"""The dataset must be opt-in.

It records the raw turns and tool traces of every saver run — a debug/dataset
artefact, not a log — so the default has to be OFF, and only an explicit
``auxiliary.dataset.enabled: true`` may turn it on. A truthy-but-not-bool value
(a string "yes", an int 1) must NOT silently start writing transcripts.
"""
import sys
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from stack import StackMemoryProvider  # noqa: E402


def _resolve(cfg):
    """Run the resolver against a fake config module returning *cfg*."""
    import types

    fake_pkg = types.ModuleType("hermes_cli")
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.load_config = lambda: cfg
    fake_pkg.config = fake_cfg
    with patch.dict(sys.modules, {"hermes_cli": fake_pkg, "hermes_cli.config": fake_cfg}):
        return StackMemoryProvider._resolve_aux_bool("dataset", "enabled", default=False)


def test_unset_defaults_off():
    assert _resolve({}) is False


def test_missing_section_defaults_off():
    assert _resolve({"auxiliary": {}}) is False


def test_explicit_true_enables():
    assert _resolve({"auxiliary": {"dataset": {"enabled": True}}}) is True


def test_explicit_false_stays_off():
    assert _resolve({"auxiliary": {"dataset": {"enabled": False}}}) is False


def test_truthy_non_bool_is_ignored():
    # ints and strings must not flip the gate — only a real bool counts.
    assert _resolve({"auxiliary": {"dataset": {"enabled": 1}}}) is False
    assert _resolve({"auxiliary": {"dataset": {"enabled": "true"}}}) is False


def test_unimportable_config_falls_back_to_default():
    with patch.dict(sys.modules, {"hermes_cli.config": None}):
        assert StackMemoryProvider._resolve_aux_bool("dataset", "enabled", default=False) is False
