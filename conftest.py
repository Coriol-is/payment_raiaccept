"""Make the addon's Odoo-free helper module importable by pytest.

`payment_raiaccept/raiaccept.py` deliberately imports nothing from
Odoo, so the protocol tests under `tests/` can run in a bare Python
environment. Importing it as part of the addon package would pull in
`models/`, which does need Odoo, so it is loaded straight from its path.
"""

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
FIXTURES = ROOT / "payment_raiaccept" / "tests" / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "raiaccept", ROOT / "payment_raiaccept" / "raiaccept.py"
)
raiaccept = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(raiaccept)
sys.modules["raiaccept"] = raiaccept


def load_fixture(name):
    """Return a parsed JSON fixture captured from the official docs."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def manifest():
    """Return the addon manifest as a dict."""
    import ast

    text = (ROOT / "payment_raiaccept" / "__manifest__.py").read_text(
        encoding="utf-8"
    )
    return ast.literal_eval(text)
