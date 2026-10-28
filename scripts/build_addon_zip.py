#!/usr/bin/env python3
"""Build an installable Kodi add-on zip.

A Kodi zip must contain the add-on folder as its single top-level entry, e.g.
``plugin.video.external.library/addon.xml`` and not ``addon.xml``. The version
is read from ``addon.xml``, so the zip name always matches what Kodi will show
after installing it.

Usage::

    python scripts/build_addon_zip.py                # -> dist/<addon>-<version>.zip
    python scripts/build_addon_zip.py --list         # show what would be included
    python scripts/build_addon_zip.py --output-dir . # write next to the repo root

The module is also importable::

    from build_addon_zip import AddonZipBuilder

    builder = AddonZipBuilder()
    print(builder.version)
    builder.build()
"""

import argparse
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Set

BASE_DIR = Path(__file__).resolve().parent.parent
DESCRIPTION = 'Build an add-on zip that Kodi can install.'

# Never ship these into the zip: build/cache artefacts and VCS metadata.
EXCLUDED_DIRS = {'.git', '.github', '__pycache__', '.venv', 'venv', '.idea'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo'}
EXCLUDED_NAMES = {'.DS_Store', 'Thumbs.db'}


class AddonZipBuilder:
    """Collects an add-on folder into a zip Kodi can install."""

    def __init__(self, addon_dir: Path, output_dir: Path = BASE_DIR / 'dist') -> None:
        self.addon_dir = Path(addon_dir)
        self.output_dir = Path(output_dir)
        if not (self.addon_dir / 'addon.xml').is_file():
            raise FileNotFoundError(f'No addon.xml in {self.addon_dir}')

    @classmethod
    def for_repo(cls, output_dir: Path = BASE_DIR / 'dist') -> 'AddonZipBuilder':
        """Locate the single add-on folder inside the repository root."""
        candidates = sorted(
            path for path in BASE_DIR.iterdir()
            if path.is_dir() and (path / 'addon.xml').is_file()
        )
        if len(candidates) != 1:
            raise FileNotFoundError(
                f'Expected exactly one add-on folder in {BASE_DIR}, found {len(candidates)}'
            )
        return cls(candidates[0], output_dir)

    def _attribute(self, name: str) -> str:
        value = ET.parse(self.addon_dir / 'addon.xml').getroot().get(name)
        if value is None:
            raise ValueError(f'addon.xml has no "{name}" attribute')
        return value

    @property
    def addon_id(self) -> str:
        """The add-on id, which is also its folder name inside the zip."""
        return self._attribute('id')

    @property
    def version(self) -> str:
        """The version declared in addon.xml."""
        return self._attribute('version')

    @property
    def zip_path(self) -> Path:
        """Where the zip will be written."""
        return self.output_dir / f'{self.addon_id}-{self.version}.zip'

    def _is_excluded(self, path: Path) -> bool:
        relative = path.relative_to(self.addon_dir)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            return True
        if path.name in EXCLUDED_NAMES:
            return True
        return path.suffix in EXCLUDED_SUFFIXES

    def collect(self) -> List[Path]:
        """Every file that belongs in the zip, in a stable order."""
        return sorted(
            (path for path in self.addon_dir.rglob('*')
             if path.is_file() and not self._is_excluded(path)),
            key=lambda path: path.as_posix(),
        )

    def build(self) -> Path:
        """Write the zip and return its path."""
        files: List[Path] = self.collect()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                arcname = (Path(self.addon_id) /
                           path.relative_to(self.addon_dir)).as_posix()
                archive.write(path, arcname)
        return self.zip_path

    def verify(self, names: Optional[Set[str]] = None) -> None:
        """Fail loudly if the zip is not shaped the way Kodi requires."""
        names = names if names is not None else set(self._read_names())
        tops = {name.split('/')[0] for name in names}
        if tops != {self.addon_id}:
            raise ValueError(f'Zip must have one top-level folder, found {sorted(tops)}')
        if f'{self.addon_id}/addon.xml' not in names:
            raise ValueError('Zip is missing addon.xml at its top level')

    def _read_names(self) -> List[str]:
        with zipfile.ZipFile(self.zip_path) as archive:
            return archive.namelist()


def main(argv: Optional[List[str]] = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--output-dir', type=Path, default=BASE_DIR / 'dist',
                        help='where to write the zip (default: <repo>/dist)')
    parser.add_argument('--list', action='store_true',
                        help='only list the files that would be included')
    args = parser.parse_args(argv)

    try:
        builder = AddonZipBuilder.for_repo(args.output_dir)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1

    files = builder.collect()
    if args.list:
        print(f'{builder.addon_id} {builder.version}: {len(files)} files')
        for path in files:
            print(f'  {path.relative_to(builder.addon_dir).as_posix()}')
        return 0

    zip_path = builder.build()
    builder.verify()
    print(f'Built {zip_path}')
    print(f'  {len(files)} files, {zip_path.stat().st_size} bytes')
    print(f'  top-level entry: {builder.addon_id}/')
    return 0


if __name__ == '__main__':
    sys.exit(main())
