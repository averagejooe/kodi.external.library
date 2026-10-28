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
"""A local SQLite mirror of the remote video library

The addon used to re-fetch the whole Movies / TV Shows list over JSON-RPC on every
open, which is slow on a Fire TV Stick. This module keeps a local copy so that a list
can be drawn from disk, and only the differences are fetched from the remote.

The remote Kodi stays the authority for watched state: ``playcount``, ``resume`` and
``lastplayed`` are refreshed from it on every open and are never trusted from the
stored copy alone.
"""

import json
import logging
import sqlite3
from contextlib import closing
from hashlib import md5
from time import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import xbmcgui

from libs.content_type_handlers import MoviesHandler, TvShowsHandler
from libs.exceptions import RemoteKodiError
from libs.kodi_service import ADDON_ID, ADDON_PROFILE_DIR, GettextEmulator, get_remote_kodi_url

logger = logging.getLogger(__name__)

_ = GettextEmulator.gettext

DB_PATH = ADDON_PROFILE_DIR / 'media_mirror.sqlite3'
SCHEMA_VERSION = 1

# Transport chunk size for the initial build only. It never surfaces as pagination:
# the list is always presented as one single directory.
BUILD_CHUNK_SIZE = 100

# How many ids go into one "IN (...)" lookup when enriching rows from another source. Kept
# well under SQLite's bound-variable limit so a long list is a few small queries, not one that
# fails.
ID_LOOKUP_CHUNK = 500


class RemoteLibraryUnavailable(RemoteKodiError):
    """The remote Kodi library could not be reached"""


class MirrorError(Exception):
    """The local library copy could not be read or written"""


class MirrorStore:
    """
    Local copy of a remote media library

    Lists are fetched once and stored, then kept in step with the remote by comparing
    item ids: new ids are fetched in full, vanished ids are deleted, and volatile fields
    are refreshed for everything else.
    """

    def __init__(self, handler):
        self._content_type = handler.content
        self._id_field = f'{handler.mediatype}id'
        self._api_class = handler.api_class

    # -- storage ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        return connection

    def _host_key(self) -> str:
        remote_url = get_remote_kodi_url(with_credentials=False)
        return md5(remote_url.encode('utf-8')).hexdigest()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                content_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                playcount INTEGER,
                resume_position REAL,
                resume_total REAL,
                lastplayed TEXT,
                dateadded TEXT,
                episode INTEGER,
                season INTEGER,
                watchedepisodes INTEGER,
                synced_at REAL NOT NULL,
                PRIMARY KEY (content_type, item_id)
            );
            CREATE TABLE IF NOT EXISTS meta (
                content_type TEXT PRIMARY KEY,
                host_key TEXT,
                schema_version INTEGER,
                complete INTEGER,
                synced_at REAL
            );
            """
        )

    def _prepare(self, connection: sqlite3.Connection) -> None:
        """Create the schema if needed, discarding a copy that belongs to another setup"""
        self._create_schema(connection)
        row = connection.execute(
            'SELECT host_key, schema_version FROM meta WHERE content_type = ?',
            (self._content_type,),
        ).fetchone()
        if row is None:
            return
        if row['schema_version'] != SCHEMA_VERSION or row['host_key'] != self._host_key():
            logger.info('Discarding the local mirror for %s: host or format changed',
                        self._content_type)
            connection.execute('DELETE FROM items WHERE content_type = ?', (self._content_type,))
            connection.execute('DELETE FROM meta WHERE content_type = ?', (self._content_type,))
            connection.commit()

    def _recreate(self) -> None:
        """Last resort for a corrupt database file"""
        logger.warning('Recreating the local mirror database')
        DB_PATH.unlink(missing_ok=True)
        with closing(self._connect()) as connection:
            self._create_schema(connection)
            connection.commit()

    def _is_complete(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            'SELECT complete FROM meta WHERE content_type = ?', (self._content_type,)
        ).fetchone()
        return bool(row and row['complete'])

    def _set_complete(self, connection: sqlite3.Connection, complete: bool) -> None:
        """Record whether the stored copy holds the whole library"""
        connection.execute(
            'INSERT OR REPLACE INTO meta '
            '(content_type, host_key, schema_version, complete, synced_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (self._content_type, self._host_key(), SCHEMA_VERSION, int(complete), time()),
        )

    # -- writing ---------------------------------------------------------------

    @staticmethod
    def _volatile_values(content_type: str, media_info: Dict[str, Any]) -> Dict[str, Any]:
        resume = media_info.get('resume') or {}
        values: Dict[str, Any] = {
            'playcount': media_info.get('playcount'),
            'resume_position': resume.get('position'),
            'resume_total': resume.get('total'),
            'lastplayed': media_info.get('lastplayed'),
            'dateadded': media_info.get('dateadded'),
        }
        if content_type == 'tvshows':
            values.update(
                episode=media_info.get('episode'),
                season=media_info.get('season'),
                watchedepisodes=media_info.get('watchedepisodes'),
            )
        return values

    @staticmethod
    def _upsert(connection: sqlite3.Connection, content_type: str, media_info: Dict[str, Any],
                id_field: str) -> None:
        values = MirrorStore._volatile_values(content_type, media_info)
        columns = ['content_type', 'item_id', 'payload', 'synced_at',
                   'playcount', 'resume_position', 'resume_total',
                   'lastplayed', 'dateadded', 'episode', 'season', 'watchedepisodes']
        placeholders = ', '.join('?' * len(columns))
        connection.execute(
            f'INSERT OR REPLACE INTO items ({", ".join(columns)}) VALUES ({placeholders})',
            (
                content_type,
                media_info[id_field],
                json.dumps(media_info),
                time(),
                values['playcount'],
                values['resume_position'],
                values['resume_total'],
                values['lastplayed'],
                values['dateadded'],
                values.get('episode'),
                values.get('season'),
                values.get('watchedepisodes'),
            ),
        )

    @staticmethod
    def _update_volatile(connection: sqlite3.Connection, content_type: str,
                         media_info: Dict[str, Any], id_field: str) -> None:
        values = MirrorStore._volatile_values(content_type, media_info)
        assignments = ', '.join(f'{name} = :{name}' for name in values)
        parameters = dict(values)
        parameters['content_type'] = content_type
        parameters['item_id'] = media_info[id_field]
        connection.execute(
            f'UPDATE items SET {assignments} '
            'WHERE content_type = :content_type AND item_id = :item_id',
            parameters,
        )

    # -- reading ---------------------------------------------------------------

    @staticmethod
    def _deserialize(rows: List[sqlite3.Row], content_type: str) -> List[Dict[str, Any]]:
        """Rebuild media info dicts from the stored rows

        Volatile fields are read from their columns so the freshest remote values win
        over whatever the payload happened to contain when it was stored.
        """
        items = []
        for row in rows:
            media_info = json.loads(row['payload'])
            media_info['playcount'] = row['playcount']
            if row['resume_position'] is not None or row['resume_total'] is not None:
                media_info['resume'] = {
                    'position': row['resume_position'] or 0.0,
                    'total': row['resume_total'] or 0.0,
                }
            media_info['lastplayed'] = row['lastplayed']
            media_info['dateadded'] = row['dateadded']
            if content_type == 'tvshows':
                media_info['episode'] = row['episode']
                media_info['season'] = row['season']
                media_info['watchedepisodes'] = row['watchedepisodes']
            items.append(media_info)
        return items

    @staticmethod
    def _default_order(media_info: Dict[str, Any]) -> str:
        """Sort key matching the remote's default 'label ascending' ordering"""
        return (media_info.get('label') or media_info.get('title') or '').lower()

    def _read_all(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute(
            'SELECT * FROM items WHERE content_type = ?', (self._content_type,)
        ).fetchall()
        items = self._deserialize(rows, self._content_type)
        items.sort(key=self._default_order)
        return items

    def _read_by_id(self, connection: sqlite3.Connection,
                    item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """The stored payloads for a set of item ids, keyed by id

        Used to enrich rows that come from another source (a smart playlist) with the full
        metadata already stored locally. Ids the copy does not hold are simply absent from the
        result - the caller falls back to the leaner source data for those, never a missing row.
        """
        if not item_ids:
            return {}
        full_by_id: Dict[int, Dict[str, Any]] = {}
        ids = list(item_ids)
        # A long playlist can exceed SQLite's bound-variable limit in one IN (...), so the ids
        # are looked up in bounded batches.
        for start in range(0, len(ids), ID_LOOKUP_CHUNK):
            batch = ids[start:start + ID_LOOKUP_CHUNK]
            placeholders = ', '.join('?' * len(batch))
            rows = connection.execute(
                f'SELECT * FROM items WHERE content_type = ? AND item_id IN ({placeholders})',
                (self._content_type, *batch),
            ).fetchall()
            full_by_id.update(
                {row['item_id']: media_info
                 for row, media_info in zip(rows, self._deserialize(rows, self._content_type))}
            )
        return full_by_id

    # -- fetching --------------------------------------------------------------

    def _fetch_light(self) -> List[Dict[str, Any]]:
        light_api = self._api_class.get_light()
        return light_api.get_media_items()

    def _fetch_new_item(self, item_id: int) -> Dict[str, Any]:
        details_api = self._api_class.get_item_details(item_id)
        return details_api.get_item()

    # -- public interface ------------------------------------------------------

    def has_local_copy(self) -> bool:
        """Whether a complete local copy exists and can be used without building"""
        try:
            with closing(self._connect()) as connection:
                self._prepare(connection)
                return self._is_complete(connection)
        except sqlite3.DatabaseError:
            self._recreate()
            return False
        except OSError as exc:
            logger.exception('Cannot read the local library copy: %s', exc)
            raise MirrorError(str(exc)) from exc

    def build(self, progress_callback: Optional[Callable[[int, int], bool]] = None) -> bool:
        """
        Fetch the whole library and store it locally

        :param progress_callback: called as ``callback(fetched, total)`` after each chunk,
            where ``total`` is the remote item count reported by the API
        :return: True when the copy was built, False when the user cancelled
        """
        logger.info('Building the local mirror for %s', self._content_type)
        try:
            with closing(self._connect()) as connection:
                self._prepare(connection)
                self._set_complete(connection, complete=False)
                connection.commit()
                offset = 0
                while True:
                    chunk_api = self._api_class(self._content_type)
                    chunk_api.item_start = offset
                    chunk_api.item_limit = BUILD_CHUNK_SIZE
                    reply = chunk_api.send_json_rpc()
                    try:
                        items = reply['result'][self._content_type]
                        remote_total = reply['result'].get('limits', {}).get('total', 0)
                    except KeyError as exc:
                        raise RemoteLibraryUnavailable(
                            f'Unexpected reply for {self._content_type}') from exc
                    for media_info in items:
                        self._upsert(connection, self._content_type, media_info, self._id_field)
                    connection.commit()
                    if not items:
                        break
                    offset += len(items)
                    if progress_callback:
                        if not progress_callback(offset, remote_total):
                            logger.info('Building the local mirror for %s was cancelled',
                                        self._content_type)
                            return False
                    if remote_total and offset >= remote_total:
                        break
                self._set_complete(connection, complete=True)
                connection.commit()
        except sqlite3.DatabaseError as exc:
            self._recreate()
            raise MirrorError(str(exc)) from exc
        except OSError as exc:
            logger.exception('Cannot write the local library copy: %s', exc)
            raise MirrorError(str(exc)) from exc
        logger.info('Finished building the local mirror for %s', self._content_type)
        return True

    def sync_and_load(self) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Bring the local copy up to date and return the list to display

        :return: a tuple of ``(media_items, fresh)``. ``fresh`` is False when the remote
            could not be contacted, meaning the returned list is the stored copy and
            watched state must not be written back.
        """
        try:
            with closing(self._connect()) as connection:
                self._prepare(connection)
                try:
                    remote_items = self._fetch_light()
                except (RemoteKodiError, RemoteLibraryUnavailable):
                    logger.warning('Cannot reach the remote Kodi host, using the stored %s list',
                                   self._content_type)
                    return self._read_all(connection), False
                self._apply_diff(connection, remote_items)
                connection.commit()
                return self._read_all(connection), True
        except sqlite3.DatabaseError as exc:
            self._recreate()
            raise MirrorError(str(exc)) from exc
        except OSError as exc:
            logger.exception('Cannot use the local library copy: %s', exc)
            raise MirrorError(str(exc)) from exc

    @staticmethod
    def _same_item(stored_payload: str, remote_file: Optional[str]) -> bool:
        """Whether a stored row still describes the item the remote now has under this id

        A library id is only the host database's row number: deleting the highest id lets it
        be handed to a newly added item, so a matching id does not prove a matching item. The
        stored file path is the tiebreaker - the same check the playlist rows make. A remote
        reply that carries no path cannot be checked, so it counts as a match and the row
        refreshes as before.
        """
        if not remote_file:
            return True
        stored_file = json.loads(stored_payload).get('file')
        return (stored_file or '').rstrip('/\\').lower() == remote_file.rstrip('/\\').lower()

    def _apply_diff(self, connection: sqlite3.Connection,
                    remote_items: List[Dict[str, Any]]) -> None:
        remote_ids = {item[self._id_field] for item in remote_items}
        stored_payloads = {
            row['item_id']: row['payload'] for row in connection.execute(
                'SELECT item_id, payload FROM items WHERE content_type = ?', (self._content_type,)
            )
        }
        for media_info in remote_items:
            item_id = media_info[self._id_field]
            stored_payload = stored_payloads.get(item_id)
            if stored_payload is not None and self._same_item(
                    stored_payload, media_info.get('file')):
                self._update_volatile(connection, self._content_type, media_info, self._id_field)
                continue
            if stored_payload is not None:
                logger.debug('Reused %s id %s, replacing its stored details',
                             self._content_type, item_id)
            else:
                logger.debug('New %s %s, fetching its details', self._content_type, item_id)
            self._upsert(connection, self._content_type, self._fetch_new_item(item_id),
                         self._id_field)
        for vanished_id in set(stored_payloads) - remote_ids:
            logger.debug('Removing %s %s from the local mirror', self._content_type, vanished_id)
            connection.execute(
                'DELETE FROM items WHERE content_type = ? AND item_id = ?',
                (self._content_type, vanished_id),
            )
        self._set_complete(connection, complete=True)


# -- shared build entry point --------------------------------------------------

# Only the two primary lists have a mirror. Recent lists, seasons and episodes do not, so a
# build request for anything else is ignored rather than creating an empty store.
PRIMARY_STORES = {
    'movies': MoviesHandler,
    'tvshows': TvShowsHandler,
}


# A playlist item's ``type`` mapped to the mirror that stores that kind of item. Episodes are
# absent on purpose: they are not mirrored, so episode rows on a playlist stay lean.
PLAYLIST_TYPE_CONTENT = {'movie': 'movies', 'tvshow': 'tvshows'}


def make_store_for(content_type: str) -> Optional[MirrorStore]:
    """The mirror store for a primary content type, or None if it has none"""
    handler_class = PRIMARY_STORES.get(content_type)
    return MirrorStore(handler_class()) if handler_class else None


def load_full_by_id(items) -> Dict[int, Dict[str, Any]]:
    """
    The full stored payload for each of ``items`` the local copy holds, keyed by item id

    ``items`` are playlist entries (each with ``id`` and ``type``). They are grouped by the
    mirror that holds that type and looked up in one query per type, so a later open costs no
    host round trip. Items with no local copy, or a type that is not mirrored, are simply
    absent from the result and the caller draws them lean.
    """
    by_content: Dict[str, set] = {}
    for item in items:
        content_type = PLAYLIST_TYPE_CONTENT.get(item.get('type'))
        if content_type is not None and item.get('id') is not None:
            by_content.setdefault(content_type, set()).add(item['id'])
    full_by_id: Dict[int, Dict[str, Any]] = {}
    for content_type, ids in by_content.items():
        store = make_store_for(content_type)
        if store is None or not store.has_local_copy():
            continue
        try:
            with closing(store._connect()) as connection:  # pylint: disable=protected-access
                full_by_id.update(store._read_by_id(connection, list(ids)))  # pylint: disable=protected-access
        except (sqlite3.DatabaseError, OSError):
            logger.exception('Cannot read the local copy for %s', content_type)
    return full_by_id


def build_mirror_with_progress(content_type: str, dialog) -> bool:
    """
    Make sure a complete local copy of one primary list exists, with a cancellable dialog

    Shared by the list code (a build on first open) and the context-menu command (an explicit
    build), so the two never drift apart. When a complete copy is already present this is just
    the every-open sync - new ids fetched, vanished ids removed - which is cheap; only a
    missing (or incomplete) copy pays for a full fetch.

    :return: True when the copy is ready to be read, False when it is not
    """
    store = make_store_for(content_type)
    if store is None:
        logger.debug('No local copy exists for %s', content_type)
        return False
    if store.has_local_copy():
        store.sync_and_load()
        return True
    heading = _('Building the local library copy')
    progress = xbmcgui.DialogProgress()
    progress.create(heading)

    def on_progress(fetched, total):
        if total:
            percent = int(fetched * 100 / total)
            progress.update(percent, f'{heading} {fetched}/{total}')
        else:
            progress.update(0, f'{heading} {fetched}')
        return not progress.iscanceled()

    try:
        return store.build(progress_callback=on_progress)
    except MirrorError as exc:
        logger.exception('Unable to build the local library copy: %s', exc)
        dialog.notification(ADDON_ID, _('Unable to store a local copy of the remote library!'),
                            icon=xbmcgui.NOTIFICATION_ERROR)
        return False
    finally:
        progress.close()
