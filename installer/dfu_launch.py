#!/usr/bin/env python3
"""Runs NumWorks' dfu.py (numworks/dfu.py) with a libusb that works on macOS
and on Windows.

dfu.py uses its own copy of pyusb, which needs the libusb-1.0 library:
  - macOS: Homebrew's (brew install libusb), in /opt/homebrew/lib or
    /usr/local/lib;
  - Windows: the DLL shipped by the "libusb-package" Python package
    (python -m pip install libusb-package). The calculator declares itself
    as a WinUSB device, so Windows needs no extra driver.

Usage: dfu_launch.py <dfu.py arguments>
"""

import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NUMWORKS = HERE / "numworks"


def add_library_dir(directory):
    directory = str(directory)
    os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(directory)
        except OSError:
            pass


def setup_libusb():
    if os.name == "nt":
        try:
            import libusb_package  # noqa: F401

            path = libusb_package.get_library_path()
            if path:
                add_library_dir(Path(str(path)).parent)
        except ImportError:
            pass
    else:
        extra = [d for d in ("/opt/homebrew/lib", "/usr/local/lib") if os.path.isdir(d)]
        if extra:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(
                extra + [os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")])
            # ctypes' find_library looks at these directories too
            for d in extra:
                add_library_dir(d)


def main():
    setup_libusb()
    sys.path.insert(0, str(NUMWORKS))
    sys.argv = [str(NUMWORKS / "dfu.py")] + sys.argv[1:]
    runpy.run_path(str(NUMWORKS / "dfu.py"), run_name="__main__")


if __name__ == "__main__":
    main()
