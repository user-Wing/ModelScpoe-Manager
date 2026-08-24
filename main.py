import sys
from pathlib import Path


bundled_site_packages = Path(__file__).resolve().parent / "runtime" / "Lib" / "site-packages"
if bundled_site_packages.is_dir():
    sys.path.insert(0, str(bundled_site_packages))

from modelscope_manager.app import run


if __name__ == "__main__":
    raise SystemExit(run())
