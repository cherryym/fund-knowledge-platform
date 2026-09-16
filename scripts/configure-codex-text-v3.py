"""Create/verify a NEW v3 offline candidate; never install it or change v2."""
import importlib.util
from pathlib import Path


def main(argv=None):
    path = Path(__file__).with_name("probe-codex-reasoning-v3.py")
    spec = importlib.util.spec_from_file_location("fkb_reasoning_v3_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
