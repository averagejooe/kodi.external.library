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
# pylint: disable=broad-exception-caught,attribute-defined-outside-init
"""Playback progress monitor"""

import json
import logging
from datetime import datetime
from urllib.parse import quote, unquote

# Intentional deviation from the original author's behavior: meaningful playback refreshes
# lastplayed even when it does not reach the watched threshold, matching Kodi's native behavior.

import xbmc

from libs import json_rpc_api
from libs.kodi_service import ADDON, ADDON_ID
from libs.mem_storage import MemStorage

logger = logging.getLogger(__name__)

# Kodi may expose the resolved stream before it exposes the original /vfs/ item. Retry the
# identity lookup on the existing one-second service tick, but never wait indefinitely.
PLAYBACK_MATCH_RETRIES = 10

# Asked of Player.GetItem. 'label' and 'type' are always present on the answer and do not need
# asking for; 'label' is what the third matching pass falls back to.
PLAYER_ITEM_PROPERTIES = ['file', 'title', 'showtitle', 'season', 'episode']


def _normalize(path):
    """A path reduced to a comparable form: no trailing separator, case-folded

    The host may report the same file with a trailing separator or different case than the
    library stores it, so the exact comparison in ``_get_item_info`` compares normalized
    values. Same normalization the playlist path comparison uses.
    """
    return (path or '').rstrip('/\\').lower()


def _file_name(path):
    """The last path component of a URL or path, without any ``.strm`` wrapper

    The third matching pass compares by file name. A row's file is the library's ``.strm``
    path; a player that resolved the stream through another add-on reports that addon's own
    URL, whose last component is the media file inside it. Both reduce to the same name here.
    The last component is unquoted first because a URL carries the name encoded (``%20``,
    ``%27``), while a row's path is not. ``.strm`` is stripped so a ``.strm`` path and the file
    it points to agree.
    """
    name = unquote((path or '').rstrip('/\\').replace('\\', '/').rsplit('/', 1)[-1])
    return name[:-5] if name.lower().endswith('.strm') else name


class PlayMonitor(xbmc.Player):
    """
    Monitors playback status and updates watches status
    for an episode or a movie from an external library
    """
    def __init__(self):
        super().__init__()
        self._mem_storage = MemStorage()
        self._clear_state()

    def _clear_state(self):
        self.is_monitoring = False
        self._current_time = -1
        self._total_time = -1
        self._playing_file = None
        self._item_info = None
        self._pending_match_retries = 0

    def onPlayBackStarted(self):
        self._playing_file = self.getPlayingFile()
        pending_key = f'__{ADDON_ID}_pending_playback__'
        pending_item = self._mem_storage.get(pending_key)
        self._mem_storage[pending_key] = None
        if pending_item and pending_item.get('item_id_param') in ('episodeid', 'movieid'):
            self._item_info = pending_item
            self.is_monitoring = True
            try:
                self._total_time = self.getTotalTime()
            except Exception:
                self._total_time = -1
            media_type = {'episodeid': 'episode', 'movieid': 'movie'}[
                pending_item['item_id_param']]
            logger.debug('Started monitoring %s via %s handoff',
                         self._playing_file, media_type)
            return
        self._pending_match_retries = PLAYBACK_MATCH_RETRIES
        self._try_match_playback()

    def _try_match_playback(self):
        current_file = self._get_playing_file()
        player_item = self._get_player_item()
        reported_file = player_item.get('file') or None
        reported_label = player_item.get('label') or None
        self._item_info = self._get_item_info(reported_file, current_file or '', reported_label)
        if self._item_info is None:
            return False
        self.is_monitoring = True
        try:
            self._total_time = self.getTotalTime()
        except Exception:
            self._total_time = -1
        logger.debug('Started monitoring %s', self._playing_file)
        return True

    def retry_pending_match(self):
        """Retry item identification during the same playback, not a second play.

        The service calls this once per second while Kodi is playing video. A resolved Drive
        URL can be reported before Kodi exposes the original /vfs/ item; keeping the playback
        pending lets a later Player.GetItem answer identify the row without losing its progress.
        """
        if self.is_monitoring or self._pending_match_retries <= 0:
            return
        if not self.isPlayingVideo():
            self._clear_state()
            return
        self._pending_match_retries -= 1
        self._try_match_playback()
        if self._item_info is None and self._pending_match_retries == 0:
            self._clear_state()

    def onPlayBackStopped(self):
        self._send_played_file_state()
        logger.debug('Stopped monitoring %s. Playback stopped.', self._playing_file)
        self._clear_state()

    def onPlayBackEnded(self):
        self._send_played_file_state()
        logger.debug('Stopped monitoring %s. Playback ended.', self._playing_file)
        self._clear_state()

    def onPlayBackPaused(self):
        if self._should_send_resume():
            self._send_resume()
        logger.debug('Paused monitoring %s', self._playing_file)

    def update_time(self):
        try:
            self._current_time = self.getTime()
        except Exception:
            self._current_time = -1
        if self._total_time <= 0:
            try:
                self._total_time = self.getTotalTime()
            except Exception:
                self._total_time = -1

    def _get_playing_file(self):
        """``getPlayingFile()`` at this moment, or None when it cannot be asked

        The value is read again on every match attempt rather than trusted from the start of
        playback: a resolved stream may be reported before Kodi exposes the original item.
        """
        try:
            return self.getPlayingFile()
        except Exception:  # pylint: disable=broad-except
            return None

    @staticmethod
    def _get_player_item():
        """What the client's own Kodi reports about the item being played, as a dict

        Two sources are asked because they answer different questions. ``getPlayingFile()``
        returns the *resolved stream*: for an item played by another add-on (a gdrive .strm)
        that is the googleapis URL, whose text shares nothing with the path the list was built
        from, so the match fails. ``Player.GetItem`` instead reports the item's own path -
        for that same strm the host-reported value is the ``/vfs/`` URL, which still contains
        the real path (quote-encoded inside it).

        The whole answer is returned, not just the file: the third matching pass needs ``label``,
        which is the file name the player reports for a stream resolved by another add-on. An
        empty dict means nothing was usable; that is not an error, the caller keeps
        ``getPlayingFile()`` as its other source.
        """
        request = json.dumps({
            'jsonrpc': '2.0',
            'method': 'Player.GetItem',
            'params': {'playerid': 1, 'properties': PLAYER_ITEM_PROPERTIES},
            'id': ADDON_ID,
        })
        try:
            item = json.loads(xbmc.executeJSONRPC(request))['result']['item']
        except Exception:  # pylint: disable=broad-except
            return {}
        return item if isinstance(item, dict) else {}

    @staticmethod
    def _item_path_from_vfs(url):
        """The item path carried inside a host ``/vfs/`` URL, or None

        ``Player.GetItem`` reports a cross-host item as ``<host>/vfs/<path>`` with the path
        quote-encoded (spaces as ``%20``). Everything from ``/vfs/`` on is the encoded path;
        unquoting it gives back the exact string the list rows are keyed by.
        """
        _, separator, encoded = (url or '').partition('/vfs/')
        return unquote(encoded) if separator else None

    def _get_item_info(self, reported_file=None, playing_file=None, reported_label=None):
        listing = self._mem_storage.get(f'__{ADDON_ID}_media_list__')
        if not listing:
            return None
        # The playing file observed on this attempt, not the value captured at playback start.
        # Same comparison, same precedence - only the moment it is read differs, so a path that
        # changes during playback can still match.
        playing_file = playing_file if playing_file is not None else self._playing_file
        files_on_shares = ADDON.getSettingBool('files_on_shares')
        # Pass 1 - the original matching, unchanged. Tried in full first so that whenever it
        # can find the item at all, it is the one that decides: the new source is strictly a
        # fallback and cannot override a result the addon already produced.
        for item in listing:
            if files_on_shares and item['file'] == playing_file:
                return item
            if quote(item['file']) in playing_file:
                return item
        # Pass 2 - the path Player.GetItem reported, when the playing file did not identify the
        # item. This is the case for an item played by another add-on (a gdrive .strm), where
        # getPlayingFile() is a googleapis URL sharing no text with the list's own paths, but
        # GetItem still reports the host /vfs/ URL. Matched by exact equality rather than
        # substring: the path is decoded in full, so a partial overlap cannot pick a neighbour.
        reported_path = self._item_path_from_vfs(reported_file)
        if reported_path:
            normalized = _normalize(reported_path)
            for item in listing:
                if _normalize(item['file']) == normalized:
                    return item
        # Pass 3 - the file name alone, for an item resolved by another add-on.
        #
        # Measured on the Fire TV client (kodi.log, 2026-10-21): the row's play target is
        # ``<host>/vfs/<path>.strm``, but the OneDrive add-on resolves the strm on the client
        # and Kodi reports that add-on's own ``127.0.0.1:<port>/download/...`` URL. Nothing of
        # the row's path survives - not the host, the drive, or the folders - and the URL never
        # changes during playback, so no amount of waiting finds it. The file name does survive:
        # it is the last component of both the local URL and the row's .strm path.
        #
        # A name is not an identity the way a full path is, so this pass is deliberately the
        # last one and it refuses to guess: two rows sharing the name means no match at all.
        # A wrong watched-state write is worse than a skipped one.
        candidates = self._rows_by_file_name(reported_label, playing_file, listing)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            logger.warning('Playback match is ambiguous: %s rows share the name %s',
                           len(candidates), reported_label)
        return None

    def _rows_by_file_name(self, reported_label, playing_file, listing):
        """Listing rows whose file name matches the player's reported label or playing file

        The label is preferred because it is the name the player shows; it is only for an
        answer that carries none that the playing file's last component is used instead. Both
        sides are reduced by ``_file_name`` so a ``.strm`` path and the media file it names
        compare equal.
        """
        name = _file_name(reported_label) or _file_name(playing_file)
        if not name:
            return []
        name = name.casefold()
        return [item for item in listing if _file_name(item['file']).casefold() == name]

    def _should_send_playcount(self):
        watched_threshold = ADDON.getSettingInt('watched_threshold_percent') / 100
        return (self._current_time != -1 and self._total_time > 0
                and (self._current_time / self._total_time) >= watched_threshold)

    def _send_playcount(self):
        if not self._item_info.get('playcount_trusted'):
            # The list was drawn from the local copy while the remote was unreachable, so
            # the stored playcount may be stale. Writing it back + 1 would corrupt the
            # watched state on the remote, so skip it.
            logger.warning('Skipping the playcount update for %s: its watched state was not '
                           'refreshed from the remote Kodi host', self._playing_file)
            return
        logger.debug('Updating playcount for %s %s', self._item_info, self._playing_file)
        item_id_param = self._item_info['item_id_param']
        new_playcount = self._item_info['playcount'] + 1
        if item_id_param == 'episodeid':
            json_rpc_api.update_lastplayed_and_resume(
                item_id_param,
                self._item_info[item_id_param],
                new_playcount,
                {'position': 0.0, 'total': 0.0},
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
        else:
            json_rpc_api.update_playcount(item_id_param, self._item_info[item_id_param],
                                          new_playcount)

    def _should_send_resume(self):
        return (self._current_time != -1
                and self._total_time != -1
                and self._current_time > ADDON.getSettingInt('playtime_to_skip'))

    def _send_resume(self):
        logger.debug('Updating resume for %s %s', self._item_info, self._playing_file)
        item_id_param = self._item_info['item_id_param']
        json_rpc_api.update_resume(item_id_param, self._item_info[item_id_param],
                                   self._current_time, self._total_time)

    def _send_lastplayed_and_resume(self):
        if not self._item_info.get('playcount_trusted'):
            logger.warning('Skipping the lastplayed update for %s: its watched state was not '
                           'refreshed from the remote Kodi host', self._playing_file)
            self._send_resume()
            return
        logger.debug('Updating lastplayed and resume for %s %s', self._item_info,
                     self._playing_file)
        item_id_param = self._item_info['item_id_param']
        json_rpc_api.update_lastplayed_and_resume(
            item_id_param,
            self._item_info[item_id_param],
            self._item_info.get('playcount', 0),
            {'position': self._current_time, 'total': self._total_time},
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _send_played_file_state(self):
        # The watched state is written to the remote Kodi host only. The list is NOT
        # refreshed here: Container.Refresh re-runs the entire directory build (a full
        # mirror sync plus a Python list rebuild) on the playback thread, freezing the UI
        # for several seconds every time playback ends. The remote still owns the watched
        # state, so the updated tick appears the next time the list is opened. Not
        # refreshing is a deliberate trade: an instantly-updating tick is not worth a
        # multi-second freeze on exit. Meaningful partial playback refreshes lastplayed without
        # changing playcount, and full episode playback refreshes it while incrementing playcount.
        # Movies keep their existing playcount-only full-watch request.
        if self._should_send_playcount():
            self._send_playcount()
        elif self._should_send_resume():
            self._send_lastplayed_and_resume()
