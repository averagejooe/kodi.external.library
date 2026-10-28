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
from hashlib import md5
from urllib.parse import parse_qsl, quote

import xbmc
import xbmcplugin
from xbmcgui import Dialog, ListItem, NOTIFICATION_ERROR

from libs.content_type_handlers import (
    MoviesHandler,
    RecentMoviesHandler,
    TvShowsHandler,
    RecentTvShowsHandler,
    SeasonsHandler,
    EpisodesHandler,
    RecentEpisodesHandler,
    MusicVideosHandler,
    RecentMusicVideosHandler,
    VIDEO_URL,
)
from libs.exceptions import NoDataError, RemoteKodiError
from libs.json_rpc_api import VideoLibraryScan, remote_reachable
from libs.kodi_service import ADDON, ADDON_ID, ADDON_NAME, GettextEmulator, get_plugin_url
from libs.media_info_service import set_info, set_art
from libs.mem_storage import MemStorage
from libs.mirror_store import (
    PLAYLIST_TYPE_CONTENT,
    MirrorError,
    MirrorStore,
    build_mirror_with_progress,
    load_full_by_id,
    make_store_for,
)
from libs.playlists import (
    get_playlist_items,
    is_pinned,
    list_remote_playlists,
    load_pins,
    playlist_label,
)

# The first page of a playlist is always this many items; later pages use the
# 'playlist_page_size' setting. Bounding the list bounds both the host fetch and the draw.
PLAYLIST_FIRST_PAGE_SIZE = 30

# The declared content type decides which view Kodi draws a directory with, and it is what makes
# a listing look like a native library (posters in the library view, info panel) or like a plain
# file list. 'native_library_view' is the global switch: ON declares each list's real type
# (movies / tvshows) as before, OFF declares 'videos' for every list - the generic view a plain
# file path gets, and the one the smart playlist page has always used.
GENERIC_CONTENT_TYPE = 'videos'

# The icons Kodi ships for a playlist / a plain folder.
PLAYLIST_ICON = {'icon': 'DefaultPlaylist.png', 'thumb': 'DefaultPlaylist.png'}

logger = logging.getLogger(__name__)
_ = GettextEmulator.gettext

HANDLE = int(sys.argv[1])

DIALOG = Dialog()

MEM_STORAGE = MemStorage()

# cacheToDisc for the listing being built, held in a one-element list so a handler can set it
# without a module-level 'global'. True lets Kodi keep the finished listing, so pressing Back
# into an already-drawn list is instant - Kodi answers the folder itself and the addon is not
# run again. It is on for every listing; only an empty/failed draw sets it False (see the guards
# in the listing functions), so a build that failed is never remembered as empty.
_CACHE_TO_DISC = [True]

CONTENT_TYPE_HANDLERS = {
    'movies': MoviesHandler,
    'recent_movies': RecentMoviesHandler,
    'tvshows': TvShowsHandler,
    'recent_tvshows': RecentTvShowsHandler,
    'seasons': SeasonsHandler,
    'episodes': EpisodesHandler,
    'recent_episodes': RecentEpisodesHandler,
    'music_videos': MusicVideosHandler,
    'recent_music_videos': RecentMusicVideosHandler,
}


def root():
    """Root action"""
    xbmcplugin.setPluginCategory(HANDLE,
                                 _('Kodi Medialibrary on {kodi_host}').format(
                                     kodi_host=ADDON.getSettingString('kodi_host')))
    if ADDON.getSettingBool('show_movies'):
        list_item = ListItem(f'[{_("Movies")}]')
        list_item.setArt({'icon': 'DefaultMovies.png', 'thumb': 'DefaultMovies.png'})
        list_item.addContextMenuItems(_build_mirror_menu('movies'))
        url = get_plugin_url(content_type='movies')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    if ADDON.getSettingBool('show_recent_movies'):
        list_item = ListItem(f'[{_("Recently added movies")}]')
        list_item.setArt({'icon': 'DefaultRecentlyAddedMovies.png',
                          'thumb': 'DefaultRecentlyAddedMovies.png'})
        url = get_plugin_url(content_type='recent_movies')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    if ADDON.getSettingBool('show_tvshows'):
        list_item = ListItem(f'[{_("TV Shows")}]')
        list_item.setArt({'icon': 'DefaultTVShows.png', 'thumb': 'DefaultTVShows.png'})
        list_item.addContextMenuItems(_build_mirror_menu('tvshows'))
        url = get_plugin_url(content_type='tvshows')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    if ADDON.getSettingBool('show_recent_tvshows'):
        list_item = ListItem(f'[{_("Recently added TV shows")}]')
        list_item.setArt({'icon': 'DefaultTVShows.png', 'thumb': 'DefaultTVShows.png'})
        url = get_plugin_url(content_type='recent_tvshows')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    if ADDON.getSettingBool('show_recent_episodes'):
        list_item = ListItem(f'[{_("Recently added episodes")}]')
        list_item.setArt({'icon': 'DefaultRecentlyAddedEpisodes.png',
                          'thumb': 'DefaultRecentlyAddedEpisodes.png'})
        url = get_plugin_url(content_type='recent_episodes')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    if ADDON.getSettingBool('show_music_videos'):
        list_item = ListItem(f'[{_("Music videos")}]')
        list_item.setArt({'icon': 'DefaultMusicVideos.png', 'thumb': 'DefaultMusicVideos.png'})
        url = get_plugin_url(content_type='music_videos')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    if ADDON.getSettingBool('show_recent_music_videos'):
        list_item = ListItem(f'[{_("Recently added music videos")}]')
        list_item.setArt({'icon': 'DefaultRecentlyAddedMusicVideos.png',
                          'thumb': 'DefaultRecentlyAddedMusicVideos.png'})
        url = get_plugin_url(content_type='recent_music_videos')
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    # Smart playlists live on the remote host; this opens the live list of them.
    list_item = ListItem(_('Smart Playlists'))
    list_item.setArt(PLAYLIST_ICON)
    url = get_plugin_url(action='playlists')
    xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    # A pinned playlist is a root shortcut to its items, evaluated by the host.
    for pin in load_pins():
        list_item = ListItem(pin['label'])
        list_item.setArt(PLAYLIST_ICON)
        list_item.addContextMenuItems(_pin_menu_items(pin['file']))
        url = get_plugin_url(action='playlist', playlist=pin['file'])
        xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=True)
    list_item = ListItem(_('Update remote videolibrary'))
    list_item.setArt({'icon': 'DefaultAddonsUpdates.png', 'thumb': 'DefaultAddonsUpdates.png'})
    url = get_plugin_url(action='update_library')
    xbmcplugin.addDirectoryItem(HANDLE, url, list_item, isFolder=False)


def _build_mirror_menu(content_type):
    """
    Context-menu entry that builds one primary list's local copy without opening it

    Only offered while no complete copy exists. Once it does, every open already syncs the
    copy, so the entry has nothing left to do and is not added. The build command fires a
    Container.Refresh when it finishes, so the entry disappears straight away.
    """
    store = make_store_for(content_type)
    if store is None or store.has_local_copy():
        return []
    caption = f'[COLOR=orange][B]{_("Build local copy")}[/B][/COLOR]'
    command = f'RunScript({ADDON_ID},build_mirror,{content_type})'
    return [(caption, command)]


def _get_listed_items(content_type_handler, dialog):
    """
    Return the list to display, and whether its watched state came fresh from the remote

    The remote Kodi is the authority for watched state. When it cannot be reached the
    stored copy is still shown, but ``fresh`` is False so that a stale playcount is never
    written back to the remote.
    """
    if not ADDON.getSettingBool('mirror_lists'):
        return list(content_type_handler.get_media_items()), True
    store = MirrorStore(content_type_handler)
    if not store.has_local_copy():
        if not build_mirror_with_progress(content_type_handler.content, dialog):
            return [], False
    return store.sync_and_load()


def _pin_menu_items(playlist_file):
    """The Pin / Unpin context-menu entry for a playlist path"""
    if is_pinned(playlist_file):
        caption = f'[COLOR=yellow][B]{_("Unpin from root menu")}[/B][/COLOR]'
    else:
        caption = f'[COLOR=green][B]{_("Pin to root menu")}[/B][/COLOR]'
    # The path is escaped so Kodi's RunScript argument parsing cannot split it apart (playlist
    # names contain spaces). Only the path is passed; on the other side the label is derived
    # from it, so no free-form text ever crosses the command line.
    command = f'RunScript({ADDON_ID},toggle_playlist_pin,{quote(playlist_file, safe="")})'
    return [(caption, command)]


def _declare_content_type(native_type):
    """
    Tell Kodi which content type a directory is, honouring the global view setting

    With 'native_library_view' on the list's real type is declared and Kodi draws it like a
    native library folder (the library view and its info panel). With it off every list is
    declared as generic 'videos' - the plain file-list view the smart playlist page has always
    used. The same switch therefore governs both the library and the playlist, so they render
    the same way.
    """
    if ADDON.getSettingBool('native_library_view') and native_type:
        xbmcplugin.setContent(HANDLE, native_type)
    else:
        xbmcplugin.setContent(HANDLE, GENERIC_CONTENT_TYPE)


def _media_path(value):
    """Normalize a file path for comparing across the two host calls"""
    return (value or '').rstrip('/\\').lower()


def _playlist_row(item, full_by_id):
    """The ``(url, ListItem, isFolder)`` row for one playlist entry

    When the local copy holds this item its full stored metadata and art are used and the row
    is drawn exactly like a library row. Otherwise the row falls back to the lean fields the
    playlist call itself returned. A missing local copy - or the metadata setting turned off -
    yields a leaner row, never a missing row.

    The match is by id, but an id is only the host database's row number: when an item is
    removed its number can be handed to a newly added one, so the id alone is not an identity.
    The stored payload's own file path is therefore required to agree with the playlist's.
    A path that does not agree means a reused id, and the row falls back to lean rather than
    risk showing another item's metadata.
    """
    full = full_by_id.get(item.get('id'))
    stored_path, item_path = _media_path(full and full.get('file')), _media_path(item.get('file'))
    if stored_path and item_path and stored_path != item_path:
        full = None
    media_info = full or item
    label = media_info.get('label') or media_info.get('title') or ''
    list_item = ListItem(label)
    if art := media_info.get('art'):
        if full:
            # The stored art is a raw path - the same form library rows pass to set_art(), which
            # prepends the host image endpoint and yields the full-resolution poster.
            set_art(list_item, art)
        else:
            # A playlist's own art is handed over already resolved, so it is set directly; the
            # set_art() wrapping would double-wrap it.
            list_item.setArt(art)
    item_type = item.get('type') or 'movie'
    # Set the info tag for every type, shows included: a show row is a folder, but the library's
    # own TV Shows list sets an info tag on its folder rows too, and the playlist should match.
    # Without this a show playlist has no plot/genre/year at all, whether rich or lean.
    set_info(list_item.getVideoInfoTag(), media_info, item_type)
    if item_type == 'tvshow':
        # A show opens into the existing seasons view; its tvshowid is the same library id the
        # TV Shows section uses, taken from the stored copy when present, else from the playlist.
        tvshowid = (full.get('tvshowid') or item['id']) if full else item['id']
        url = get_plugin_url(content_type='seasons', tvshowid=tvshowid, parent_category=label)
        return url, list_item, True
    # Playback always uses the playlist's own live file path, never the stored copy's.
    file_path = item.get('file')
    if ADDON.getSettingBool('files_on_shares'):
        url = file_path
    else:
        # The list only exists because the host answered, so the direct URL - the proven
        # playback path - is always used here, never the offline plugin routing.
        url = f'{VIDEO_URL}/{quote(file_path)}'
    return url, list_item, False


def show_playlist_menu():
    """List the remote host's smart playlists, each pinnable through its context menu"""
    xbmcplugin.setPluginCategory(HANDLE, _('Smart Playlists'))
    xbmcplugin.setContent(HANDLE, 'files')
    try:
        playlists = list_remote_playlists()
    except (RemoteKodiError, NoDataError) as exc:
        logger.exception('Unable to list the remote playlists: %s', exc)
        DIALOG.notification(ADDON_ID, _('Unable to connect to the remote Kodi host!'),
                            icon=NOTIFICATION_ERROR)
        return
    directory_items = []
    for playlist in playlists:
        list_item = ListItem(playlist['label'])
        list_item.setArt(PLAYLIST_ICON)
        list_item.addContextMenuItems(_pin_menu_items(playlist['file']))
        url = get_plugin_url(action='playlist', playlist=playlist['file'])
        directory_items.append((url, list_item, True))
    if not directory_items:
        DIALOG.notification(ADDON_ID, _('No playlists found on the remote Kodi host'))
    xbmcplugin.addDirectoryItems(HANDLE, directory_items, len(directory_items))


def show_playlist_items(playlist_file, page=1):
    """
    List the items of one smart playlist, one bounded page at a time

    The whole list is read from the host once and, while paging, kept in memory so later pages
    are slices of that same read. Refetching per page would reshuffle a random playlist between
    pages. With paging off the whole list is drawn at once. Either way nothing is mirrored.
    """
    xbmcplugin.setPluginCategory(HANDLE, playlist_label(playlist_file))
    paging = ADDON.getSettingBool('playlist_paging')
    # The path is hashed so the in-memory key is a plain token, never a special:// path.
    path_digest = md5(playlist_file.encode('utf-8')).hexdigest()
    cache_key = f'__{ADDON_ID}_playlist__{path_digest}'
    items = MEM_STORAGE.get(cache_key) if paging and page > 1 else None
    if items is None:
        try:
            items = get_playlist_items(playlist_file)
        except (RemoteKodiError, NoDataError) as exc:
            logger.exception('Unable to open the playlist %s: %s', playlist_file, exc)
            DIALOG.notification(ADDON_ID, _('Unable to connect to the remote Kodi host!'),
                                icon=NOTIFICATION_ERROR)
            return
        if paging:
            MEM_STORAGE[cache_key] = items
    # Declare the type the playlist holds (from its own items) so that, with the global view
    # setting on, it renders like the library - movies / tvshows view - not the generic one.
    # Items report a singular type ('movie' / 'tvshow') but setContent wants the plural content
    # string ('movies' / 'tvshows'). The short form is not a valid content type, so the skin
    # could not match its movie/tvshow view types and dropped them (no Widelist etc.). The map
    # also turns an unrecognised type into None, which falls back to the generic view.
    item_types = {PLAYLIST_TYPE_CONTENT.get(item.get('type')) for item in items}
    if len(item_types) == 1:
        _declare_content_type(item_types.pop())
    else:
        _declare_content_type(None)
    if paging:
        # Page 1 is always the fixed first page; its size may differ from later pages, so the
        # offset of page k accounts for that rather than assuming one uniform size.
        if page == 1:
            start, size = 0, PLAYLIST_FIRST_PAGE_SIZE
        else:
            size = ADDON.getSettingInt('playlist_page_size')
            start = PLAYLIST_FIRST_PAGE_SIZE + (page - 2) * size
        page_items = items[start:start + size]
        has_more = start + size < len(items)
    else:
        page_items = items
        has_more = False
    # An empty or failed page writes an empty list, so no stale playlist identity is left
    # available for playback.
    MEM_STORAGE[f'__{ADDON_ID}_playlist_playback__'] = _playlist_playback_rows(page_items)

    # The metadata setting decides whether playlist rows are enriched from the local copy. With
    # it off - or when no copy exists - every row is the lean one built from the playlist call.
    full_by_id = load_full_by_id(items) if ADDON.getSettingBool('playlist_metadata') else {}
    directory_items = [_playlist_row(item, full_by_id) for item in page_items]
    if has_more:
        list_item = ListItem(_('Next page'))
        list_item.setArt(PLAYLIST_ICON)
        url = get_plugin_url(action='playlist', playlist=playlist_file, page=page + 1)
        directory_items.append((url, list_item, True))
    xbmcplugin.addDirectoryItems(HANDLE, directory_items, len(directory_items))
    if not directory_items:
        # An empty playlist (or a failed read) is never cached, so a transient failure is not
        # remembered as an empty page until a restart.
        _CACHE_TO_DISC[0] = False


def show_media_items(content_type, tvshowid=None, season=None, parent_category=None):
    content_type_handler_class = CONTENT_TYPE_HANDLERS.get(content_type)
    if content_type_handler_class is None:
        raise RuntimeError(f'Unknown content type: {content_type}')
    content_type_handler = content_type_handler_class(tvshowid, season, parent_category)
    xbmcplugin.setPluginCategory(HANDLE, content_type_handler.get_plugin_category())
    _declare_content_type(content_type_handler.content)
    fresh = True
    try:
        if content_type_handler.mirrored:
            media_items, fresh = _get_listed_items(content_type_handler, DIALOG)
        else:
            media_items = list(content_type_handler.get_media_items())
    except NoDataError:
        logger.exception('Unable to retrieve %s from the remote Kodi library',
                         content_type)
        DIALOG.notification(ADDON_ID, _('Unable to retrieve data from the remote Kodi library!'),
                            icon=NOTIFICATION_ERROR)
        return
    except RemoteKodiError as exc:
        logger.exception('Unable to connect to %s', str(exc))
        DIALOG.notification(ADDON_ID, _('Unable to connect to the remote Kodi host!'),
                            icon=NOTIFICATION_ERROR)
        return
    except MirrorError as exc:
        logger.exception('Unable to use the local library copy: %s', exc)
        DIALOG.notification(ADDON_ID, _('Unable to use the local copy of the remote library!'),
                            icon=NOTIFICATION_ERROR)
        return
    # The host answered if the list was fetched fresh; a list served from the local copy
    # means it did not. Playable items route through the plugin for the offline check only
    # in that case, keeping the long-standing direct URL path when the host is up.
    content_type_handler.host_reachable = fresh
    logger.debug('Creating a list of %s items...', content_type)
    directory_items = []
    mem_storage_items = []
    for media_info in media_items:
        if content_type_handler.mirrored and not content_type_handler.should_show(media_info):
            continue
        list_item = ListItem(media_info.get('title') or media_info.get('label', ''))
        if art := media_info.get('art'):
            set_art(list_item, art)
        info_tag = list_item.getVideoInfoTag()
        set_info(info_tag, media_info, content_type_handler.mediatype)
        list_item.addContextMenuItems(content_type_handler.get_item_context_menu(media_info))
        directory_items.append((
            content_type_handler.get_item_url(media_info),
            list_item,
            content_type_handler.item_is_folder,
        ))
        if content_type_handler.should_save_to_mem_storage:
            item_id_param = f'{content_type_handler.mediatype}id'
            mem_storage_items.append({
                'item_id_param': item_id_param,
                item_id_param: media_info[item_id_param],
                'file': media_info['file'],
                'playcount': media_info.get('playcount', 0),
                'resume': media_info.get('resume') or {},
                # Only a value refreshed from the remote may be written back: see
                # PlayMonitor._send_playcount
                'playcount_trusted': fresh,
            })
    xbmcplugin.addDirectoryItems(HANDLE, directory_items, len(directory_items))
    MEM_STORAGE[f'__{ADDON_ID}_media_list__'] = mem_storage_items
    # A library list is cached only when the draw actually produced rows. An empty draw is not
    # cached, so a build that failed (host off) cannot be remembered as empty - which was the
    # whole reason the cache was originally disabled. Seasons and episodes legitimately have no
    # rows when the remote host is unreachable, so not caching them is also the honest result.
    if not directory_items:
        _CACHE_TO_DISC[0] = False
    for sort_method in content_type_handler.get_sort_methods():
        xbmcplugin.addSortMethod(HANDLE, sort_method)
    logger.debug('Finished creating a list of %s items.', content_type)


def play_item():
    """
    Hand a playable item to Kodi after checking the remote host is reachable

    Without this the plugin would pass Kodi a remote URL directly; when the host is offline
    the player sits on an unreachable URL until the OS socket timeout, with no message, which
    looks like a freeze. Checking first turns that into an immediate notification.
    """
    file_path = dict(parse_qsl(sys.argv[2][1:])).get('file')
    list_item = ListItem()
    if not file_path:
        logger.error('Play requested with no file in the URL')
        xbmcplugin.setResolvedUrl(HANDLE, False, list_item)
        return
    if not remote_reachable():
        DIALOG.notification(ADDON_ID, _('Unable to connect to the remote Kodi host!'),
                            icon=NOTIFICATION_ERROR)
        xbmcplugin.setResolvedUrl(HANDLE, False, list_item)
        return
    list_item.setPath(f'{VIDEO_URL}/{quote(file_path)}')
    xbmcplugin.setResolvedUrl(HANDLE, True, list_item)


def _resume_choice():
    """Return Kodi's resume choice from the plugin invocation, if it supplied one."""
    for argument in sys.argv[3:]:
        key, separator, value = argument.partition(':')
        if separator and key.lower() == 'resume':
            return value.lower() == 'true'
    return None


def _play_handoff(item_info, file_path):
    """Start the known-good direct VFS target with an optional resume position."""
    resume = item_info.get('resume') or {}
    position = float(resume.get('position') or 0.0)
    choice = _resume_choice()
    if choice is False:
        start_position = 0
    elif choice is True and position > 0:
        start_position = int(position)
    else:
        start_position = -1
    target = f'{VIDEO_URL}/{quote(file_path)}'
    xbmc.Player().play(target, None, False, start_position)


def play_episode_item():
    """Carry the host episode row into the monitor before direct playback starts."""
    pending_key = f'__{ADDON_ID}_pending_playback__'
    MEM_STORAGE[pending_key] = None
    params = dict(parse_qsl(sys.argv[2][1:]))
    file_path = params.get('file')
    episode_id = int(params['episodeid'])
    listing = MEM_STORAGE.get(f'__{ADDON_ID}_media_list__') or []
    item_info = next(item for item in listing
                     if item.get('episodeid') == episode_id and item['file'] == file_path)
    MEM_STORAGE[pending_key] = item_info
    _play_handoff(item_info, file_path)


def play_movie_item():
    """Carry the host movie row into the monitor before direct playback starts."""
    pending_key = f'__{ADDON_ID}_pending_playback__'
    MEM_STORAGE[pending_key] = None
    params = dict(parse_qsl(sys.argv[2][1:]))
    file_path = params.get('file')
    movie_id = int(params['movieid'])
    if params.get('playlist'):
        listing = MEM_STORAGE.get(f'__{ADDON_ID}_playlist_playback__') or []
    else:
        listing = MEM_STORAGE.get(f'__{ADDON_ID}_media_list__') or []
    item_info = next(item for item in listing
                     if item.get('movieid') == movie_id and item['file'] == file_path)
    MEM_STORAGE[pending_key] = item_info
    _play_handoff(item_info, file_path)


def _playlist_playback_rows(items):
    return [
        {
            'item_id_param': 'movieid',
            'movieid': item['id'],
            'file': item['file'],
            'playcount': item.get('playcount', 0),
            'resume': item.get('resume') or {},
            'playcount_trusted': True,
        }
        for item in items if (item.get('type') or 'movie') == 'movie'
    ]


def update_remote_library():
    VideoLibraryScan().send_json_rpc()
    DIALOG.ok(ADDON_NAME, _('Updating the remote videolibrary started.'))


def router(paramstring):
    params = dict(parse_qsl(paramstring))
    logger.debug('Called addon with params: %s', str(sys.argv))
    if 'content_type' in params:
        if (tvshowid := params.get('tvshowid')) is not None:
            tvshowid = int(tvshowid)
        if (season := params.get('season')) is not None:
            season = int(season)
        parent_category = params.get('parent_category')
        show_media_items(params['content_type'], tvshowid, season, parent_category)
    elif params.get('action') == 'play_episode':
        play_episode_item()
        return
    elif params.get('action') == 'play_movie':
        play_movie_item()
        return
    elif params.get('action') == 'play':
        play_item()
        return
    elif params.get('action') == 'update_library':
        update_remote_library()
        return
    elif params.get('action') == 'playlists':
        show_playlist_menu()
    elif params.get('action') == 'playlist':
        show_playlist_items(params['playlist'], int(params.get('page', 1)))
    else:
        root()
    # cacheToDisc is on for every listing: Kodi keeps the finished listing, so pressing Back
    # into an already-drawn list is instant - the addon is not run again (1.1.11 behaved this
    # way; 1.1.12 turned it off and Back redrew instead). The only listing that is NOT cached is
    # one that drew nothing: an empty or failed draw is never remembered, so a build that failed
    # is not served as empty until a restart. Note that a forward open (clicking into a folder)
    # deletes Kodi's saved copy, so this only ever speeds up Back; a first or repeated forward
    # open still draws live.
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=_CACHE_TO_DISC[0])
