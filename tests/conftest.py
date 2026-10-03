"""Pytest config — make the plugin importable as a package so relative imports work."""
import sys
from pathlib import Path

# The plugin dir IS the `stack` package (has __init__.py).
# Its parent (~/.hermes/plugins) must be on sys.path for `import stack` to work.
plugin_parent = str(Path(__file__).resolve().parent.parent.parent)
if plugin_parent not in sys.path:
    sys.path.insert(0, plugin_parent)