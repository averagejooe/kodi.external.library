# Copyright (C) 2023, Roman Miroshnychenko aka Roman V.M.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Remote Kodi smart playlists: enumerate them, read one, and pin one to the root menu

A playlist is read live from the host and is deliberately NOT mirrored - the same stance as
seasons/episodes. Everything a row needs (title, art, plot, playcount) comes back from the
single directory call, so playlists never touch the local library copy.
"""

import json
import logging
from pathlib import PurePosixPath

from libs import json_rpc_api
from libs.exceptions import NoDataError, RemoteKodiError
from libs.kodi_service import ADDON_PROFILE_DIR

logger = logging.getLogger(__name__)

# The host's video-playlists folder. Reading it lists the `.xsp` files it contains.
PLAYLISTS_DIR = 'special://videoplaylists'

# The last good enumeration, shown as a fallback when the host cannot be reached.
CACHE_PATH = ADDON_PROFILE_DIR / 'playlists_cache.json'
# Pinned playlists: the only thing this feature persists.
PINS_PATH = ADDON_PROFILE_DIR / 'playlist_pins.json'


def _read_json(path, default):
    try:
        with path.open('r', encoding='utf-8') as fo:
            return json.load(fo)
    except (OSError, ValueError):
        return default


def _write_json(path, data) -> None:
    try:
        with path.open('w', encoding='utf-8') as fo:
            json.dump(data, fo)
    except OSError:
        logger.exception('Cannot write %s', path)


def playlist_label(file: str) -> str:
    """
    The label for a playlist path

    Derived from the path itself, never from user-supplied text, so nothing that has to cross
    a RunScript command line can be arbitrary.
    """
    name = PurePosixPath(file).name
    return name[:-4] if name.lower().endswith('.xsp') else name


def list_remote_playlists():
    """
    The remote host's video smart playlists

    Enumerated live, so a playlist added on the host appears with no saved state. The last good
    result is cached and returned if the host is unreachable.
    """
    try:
        files = json_rpc_api.GetDirectory(PLAYLISTS_DIR).get_files()
    except (RemoteKodiError, NoDataError):
        logger.warning('Cannot enumerate the remote playlists, using the cached list if present')
        return _read_json(CACHE_PATH, [])
    playlists = [
        {'label': item.get('label') or playlist_label(item['file']), 'file': item['file']}
        for item in files if item.get('file')
    ]
    _write_json(CACHE_PATH, playlists)
    return playlists


def get_playlist_items(file: str):
    """
    The items a playlist's rules matched, as one list from the host

    :raises: RemoteKodiError when the host is unreachable, NoDataError on an unusable reply
    """
    return json_rpc_api.GetDirectory(file).get_files()


# -- pinned playlists (the only persisted state) ----------------------------------

def load_pins():
    """The playlists pinned to the root menu"""
    return _read_json(PINS_PATH, [])


def is_pinned(file: str) -> bool:
    """Whether a playlist path is currently pinned to the root menu"""
    return any(pin['file'] == file for pin in load_pins())


def toggle_pin(file: str) -> bool:
    """
    Pin or unpin a playlist path

    :return: the new pinned state
    """
    pins = load_pins()
    remaining = [pin for pin in pins if pin['file'] != file]
    if len(remaining) == len(pins):
        remaining.append({'label': playlist_label(file), 'file': file})
        pinned = True
    else:
        pinned = False
    _write_json(PINS_PATH, remaining)
    return pinned
