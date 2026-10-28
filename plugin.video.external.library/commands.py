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
import logging
import sys
from urllib.parse import unquote

import xbmc
import xbmcgui

from libs.exception_logger import catch_exception
from libs.json_rpc_api import update_playcount
from libs.kodi_service import GettextEmulator, initialize_logging
from libs.mirror_store import build_mirror_with_progress
from libs.playlists import toggle_pin

initialize_logging()
logger = logging.getLogger(__name__)
_ = GettextEmulator.gettext


def main():
    logger.debug('Executing command: %s', str(sys.argv))
    if len(sys.argv) == 1:
        xbmcgui.Dialog().ok(_('Kodi External Video Library Client'),
                            _(r'Please run this addon from \"Video addons\" section.'))
    elif sys.argv[1] == 'update_playcount':
        update_playcount(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
        xbmc.executebuiltin('Container.Refresh')
    elif sys.argv[1] == 'toggle_playlist_pin':
        # The playlist path arrives URL-encoded so a name with spaces cannot split the
        # RunScript arguments. Refresh re-runs the current directory, so the new pinned row
        # appears immediately (the listing is not cached by Kodi).
        toggle_pin(unquote(sys.argv[2]))
        xbmc.executebuiltin('Container.Refresh')
    elif sys.argv[1] == 'build_mirror':
        # Builds one primary list's local copy without opening the list, so the wait happens
        # on demand instead of on the first browse. Today only the two primary lists have a
        # copy; the helper ignores anything else. The refresh redraws the root so the
        # now-redundant "Build local copy" entry disappears without leaving the addon.
        if build_mirror_with_progress(sys.argv[2], xbmcgui.Dialog()):
            xbmcgui.Dialog().notification(
                _('Kodi External Video Library Client'),
                _('The local library copy is up to date.'))
            xbmc.executebuiltin('Container.Refresh')


if __name__ == '__main__':
    with catch_exception():
        main()
