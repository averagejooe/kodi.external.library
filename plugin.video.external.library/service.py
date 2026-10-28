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

import xbmc
import xbmcaddon

# TEMP DIAGNOSTIC (1.1.18): proves Kodi actually executes this file. WARNING level so it is
# written even with debug logging off, and only Kodi's own modules are imported above it, so it
# still prints if a later addon import is what fails. The version is stamped in (1.1.24) because
# two different builds were both shipped as 1.1.23, so which one was installed could not be told
# apart; this line now proves the running build at a glance. Revert together with addon.xml's
# reuselanguageinvoker change (see snapshot pre-service-diagnostic-1.1.18).
xbmc.log(f'ELIB DIAG: service.py executed version={xbmcaddon.Addon().getAddonInfo("version")}',
         level=xbmc.LOGWARNING)

from libs.exception_logger import catch_exception
from libs.kodi_service import initialize_logging
from libs.monitor import PlayMonitor

initialize_logging()
logger = logging.getLogger(__name__)

with catch_exception():
    logger.debug('Starting playback monitoring service...')
    kodi_monitor = xbmc.Monitor()
    play_monitor = PlayMonitor()
    while not kodi_monitor.waitForAbort(1.0):
        if play_monitor.isPlayingVideo():
            if not play_monitor.is_monitoring:
                play_monitor.retry_pending_match()
            elif not xbmc.getCondVisibility('Player.Paused'):
                play_monitor.update_time()
logger.debug('Stopped playback monitoring service.')
