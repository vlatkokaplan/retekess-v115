"""Locate optional development executables without vendoring platform binaries."""
import os
from pathlib import Path
import shutil


def executable(name):
    override = os.environ.get(name.upper())
    path = shutil.which(override or name)
    if path is None:
        raise FileNotFoundError(f'Install {name}, add it to PATH, or set {name.upper()} to its executable path')
    return str(Path(path).resolve())
