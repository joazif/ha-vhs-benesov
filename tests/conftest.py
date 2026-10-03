import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "vhs_api", ROOT / "custom_components" / "vhs_benesov" / "api.py"
)
api = importlib.util.module_from_spec(_spec)
sys.modules["vhs_api"] = api
_spec.loader.exec_module(api)


def fixture(name: str) -> str:
    return (pathlib.Path(__file__).parent / f"fixture_{name}.html").read_text("utf-8")
