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
"""Classes and functions responsible for interacting with the remote JSON-RPC API"""

import logging
from pprint import pformat
from typing import List, Dict, Any, Optional

import simple_requests as requests

from libs.exceptions import NoDataError, RemoteKodiError
from libs.kodi_service import ADDON, get_remote_kodi_url

logger = logging.getLogger(__name__)

# Seconds to wait for the remote Kodi host before giving up. Without this a request to an
# unreachable host can block for as long as the socket hangs - and it runs on Kodi's UI
# thread, so that shows up as the whole interface freezing. A healthy reply takes well
# under a second, so this is generous.
REQUEST_TIMEOUT = 10

# The every-open "what changed?" check uses a much shorter timeout. It runs on every list
# open, and a dead host should cost a couple of seconds there, not ten. The stored copy is
# shown either way, so giving up early only trades a small chance of a stale list for a
# noticeably faster open while the remote host is offline.
REACHABILITY_TIMEOUT = 3

# Properties requested for a Files.GetDirectory listing. For a smart playlist the host returns
# them per item, so a row can be built from this one call alone - no local mirror lookup.
DIRECTORY_PROPERTIES = ['title', 'year', 'genre', 'plot', 'art', 'playcount']


class BaseJsonRpcApi:
    kodi_url = get_remote_kodi_url(with_credentials=False)
    method: str
    timeout = REQUEST_TIMEOUT

    def send_json_rpc(self):
        """
        Send JSON-RPC to remote Kodi
        """
        request = {
            'jsonrpc': '2.0',
            'method': self.method,
            'id': '1',
        }
        params = self.get_params()  # pylint: disable=assignment-from-none
        if params is not None:
            request['params'] = params
        logger.debug('JSON-RPC request: %s', pformat(request))
        auth = None
        login = ADDON.getSetting('kodi_login')
        password = ADDON.getSetting('kodi_password')
        if login:
            auth = (login, password)
        try:
            json_reply = requests.post(self.kodi_url + '/jsonrpc', json=request,
                                       auth=auth, verify=False, timeout=self.timeout).json()
        except requests.RequestException as exc:
            raise RemoteKodiError(self.kodi_url) from exc
        logger.debug('JSON-RPC reply: %s', pformat(json_reply))
        return json_reply

    def get_params(self) -> Optional[Dict[str, Any]]:
        """Get params to send to Kodi JSON-RPC API"""
        return None


class PingRemote(BaseJsonRpcApi):
    """Cheapest possible call, used only to check the remote host is reachable"""
    method = 'JSONRPC.Ping'
    timeout = REACHABILITY_TIMEOUT


def remote_reachable() -> bool:
    """
    Whether the remote Kodi host answers

    Unlike the list build, playback cannot fall back to the local copy, so this is checked
    before handing a playable item to the player.
    """
    try:
        PingRemote().send_json_rpc()
        return True
    except RemoteKodiError:
        return False
    except Exception:  # pylint: disable=broad-except
        # An unexpected reply must not block playback that might otherwise work.
        logger.exception('Unexpected reply while checking the remote Kodi host')
        return True


class BaseMediaItemsRetriever(BaseJsonRpcApi):
    properties: List[str]
    sort: Optional[Dict[str, str]] = None
    item_limit: Optional[int] = None
    item_start = 0

    def __init__(self, content, tvshowid=None, season=None):
        self._content = content
        self._tvshowid = tvshowid
        self._season = season

    def get_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if self.properties:
            params['properties'] = self.properties
        if self.sort is not None:
            params['sort'] = self.sort
        if self.item_limit is not None:
            # Only ever used to chunk the initial mirror build over the transport.
            # It is not pagination: the list is presented as one single directory.
            params['limits'] = {
                'start': self.item_start,
                'end': self.item_start + self.item_limit,
            }
        if self._tvshowid is not None:
            params['tvshowid'] = self._tvshowid
        if self._season is not None:
            params['season'] = self._season
        return params

    def get_media_items(self) -> List[Dict[str, Any]]:
        """
        Get the list of media items for Kodi database
        
        :raises: NoDataError when media items are not retrieved via JSON-RPC
        """
        try:
            return self.send_json_rpc()['result'][self._content]
        except KeyError as exc:
            raise NoDataError(
                f'Unable to retrieve {self._content} from remote media library') from exc


class GetMovies(BaseMediaItemsRetriever):
    method = 'VideoLibrary.GetMovies'
    # DECISION: 'cast' is deliberately NOT requested. It is the dominant per-item cost
    # (20-50 actors, each with a thumbnail URL that has to be escaped and turned into an
    # xbmc.Actor object) and it is not shown in the list or grid view - only in the Movie
    # Information popup. Dropping it makes the mirror build and the per-open sync smaller
    # and faster. 'streamdetails' IS kept because it drives the media badges (codec /
    # resolution / audio) in that same popup. Lazy-loading the cast only when the info
    # popup is opened is a possible future improvement; it is not implemented.
    properties = [
        'title',
        'genre',
        'year',
        'rating',
        'director',
        'trailer',
        'tagline',
        'plot',
        'plotoutline',
        'originaltitle',
        'playcount',
        'writer',
        'studio',
        'mpaa',
        'country',
        'streamdetails',
        'top250',
        'votes',
        'file',
        'sorttitle',
        'resume',
        'dateadded',
        'lastplayed',
        'art',
        'premiered',
    ]
    sort = {'order': 'ascending', 'method': 'label'}

    @classmethod
    def get_light(cls):
        """The cheap retriever used for the every-open "what changed?" check"""
        return GetMoviesLight('movies')

    @classmethod
    def get_item_details(cls, item_id):
        """The retriever for one full item, used only for items new to the local mirror"""
        return GetMovieDetails(item_id)


class GetRecentlyAddedMovies(GetMovies):
    method = 'VideoLibrary.GetRecentlyAddedMovies'
    sort = {'order': 'descending', 'method': 'dateadded'}


class GetTVShows(BaseMediaItemsRetriever):
    method = 'VideoLibrary.GetTVShows'
    # DECISION: 'cast' is deliberately NOT requested here either - see the note on GetMovies.
    properties = [
        'title',
        'genre',
        'year',
        'rating',
        'plot',
        'studio',
        'mpaa',
        'episode',
        'playcount',
        'premiered',
        'votes',
        'file',
        'originaltitle',
        'sorttitle',
        'season',
        'watchedepisodes',
        'dateadded',
        'lastplayed',
        'tag',
        'art',
        'userrating',
        'ratings',
        'runtime',
        'uniqueid',
    ]
    sort = {'order': 'ascending', 'method': 'label'}

    @classmethod
    def get_light(cls):
        """The cheap retriever used for the every-open "what changed?" check"""
        return GetTVShowsLight('tvshows')

    @classmethod
    def get_item_details(cls, item_id):
        """The retriever for one full item, used only for items new to the local mirror"""
        return GetTVShowDetails(item_id)


class GetRecentlyAddedTVShows(GetTVShows):
    item_limit = 25
    sort = {'order': 'descending', 'method': 'dateadded'}


# The properties below are the ones the mirror has to refresh from the remote on every
# open. They are cheap compared to a full item, so the "what changed?" check stays small.
# Everything else is fetched once, when an item is first stored.
# 'file' is not here to be refreshed: it identifies. A deleted id can be handed to a
# different item, so the mirror compares this path with the stored one to tell a genuine
# refresh from a reused id (see MirrorStore._same_item).
_MOVIE_VOLATILE_PROPERTIES = ['playcount', 'resume', 'lastplayed', 'dateadded', 'file']
# 'resume' is a playable-item property: Video.Fields.TVShow does not accept it, and a
# request that includes it is rejected with InvalidParams (the reply then carries no
# 'result', which the retriever reports as "unable to retrieve tvshows"). Movies are
# playable and may ask for it; tvshows must not.
_TVSHOW_VOLATILE_PROPERTIES = [
    'playcount', 'lastplayed', 'dateadded', 'episode', 'season', 'watchedepisodes',
    'file',  # identifies, not refreshed - see the note above
]


class GetMoviesLight(GetMovies):
    """Movie ids plus the fields that must be re-read from the remote on every open"""
    properties = _MOVIE_VOLATILE_PROPERTIES
    # The volatile properties are all server-side, so let the remote do the ordering.
    sort = {'order': 'ascending', 'method': 'label'}
    timeout = REACHABILITY_TIMEOUT


class GetTVShowsLight(GetTVShows):
    """TV show ids plus the fields that must be re-read from the remote on every open"""
    properties = _TVSHOW_VOLATILE_PROPERTIES
    sort = {'order': 'ascending', 'method': 'label'}
    timeout = REACHABILITY_TIMEOUT


class BaseItemDetailsRetriever(BaseJsonRpcApi):
    """Fetch one fully detailed item, used only for items new to the local mirror"""

    method: str
    properties: List[str]
    result_key: str
    id_param: str

    def __init__(self, item_id):
        super().__init__()
        self._item_id = item_id

    def get_params(self) -> Dict[str, Any]:
        return {
            self.id_param: self._item_id,
            'properties': self.properties,
        }

    def get_item(self) -> Dict[str, Any]:
        reply = self.send_json_rpc()
        try:
            return reply['result'][self.result_key]
        except KeyError as exc:
            raise NoDataError(
                f'Unable to retrieve {self.id_param} {self._item_id} from remote media library'
            ) from exc


class GetMovieDetails(BaseItemDetailsRetriever):
    method = 'VideoLibrary.GetMovieDetails'
    result_key = 'moviedetails'
    id_param = 'movieid'
    properties = GetMovies.properties


class GetTVShowDetails(BaseItemDetailsRetriever):
    method = 'VideoLibrary.GetTVShowDetails'
    result_key = 'tvshowdetails'
    id_param = 'tvshowid'
    properties = GetTVShows.properties


class GetSeasons(BaseMediaItemsRetriever):
    method = 'VideoLibrary.GetSeasons'
    properties = [
        'season',
        'showtitle',
        'playcount',
        'episode',
        'tvshowid',
        'watchedepisodes',
        'art',
        'title',
    ]
    sort = {'order': 'ascending', 'method': 'season'}


class GetEpisodes(BaseMediaItemsRetriever):
    method = 'VideoLibrary.GetEpisodes'
    properties = [
        'title',
        'plot',
        'votes',
        'rating',
        'writer',
        'firstaired',
        'playcount',
        'runtime',
        'director',
        'productioncode',
        'season',
        'episode',
        'originaltitle',
        'showtitle',
        'cast',
        'streamdetails',
        'file',
        'resume',
        'tvshowid',
        'dateadded',
        'lastplayed',
        'uniqueid',
        'art',
        'specialsortseason',
        'specialsortepisode',
        'seasonid',
    ]
    sort = {'order': 'ascending', 'method': 'label'}


class GetRecentlyAddedEpisodes(GetEpisodes):
    method = 'VideoLibrary.GetRecentlyAddedEpisodes'
    sort = {'order': 'descending', 'method': 'dateadded'}


class GetMusicVideos(BaseMediaItemsRetriever):
    method = 'VideoLibrary.GetMusicVideos'
    properties = [
            'title',
            'playcount',
            'runtime',
            'director',
            'studio',
            'year',
            'plot',
            'album',
            'artist',
            'genre',
            'track',
            'streamdetails',
            'lastplayed',
            'file',
            'resume',
            'dateadded',
            'art',
            'rating',
            'premiered',
        ]
    sort = {'order': 'ascending', 'method': 'label'}


class GetRecentlyAddedMusicVideos(GetMusicVideos):
    method = 'VideoLibrary.GetRecentlyAddedMusicVideos'
    sort = {'order': 'descending', 'method': 'dateadded'}


class SetMovieDetails(BaseJsonRpcApi):
    method = 'VideoLibrary.SetMovieDetails'

    def __init__(self):
        super().__init__()
        self._params = None

    def get_params(self) -> Dict[str, Any]:
        return self._params

    def set_details(self, **kwargs):
        self._params = kwargs
        self.send_json_rpc()


class SetEpisodeDetails(SetMovieDetails):
    method = 'VideoLibrary.SetEpisodeDetails'


class VideoLibraryScan(BaseJsonRpcApi):
    method = 'VideoLibrary.Scan'


class GetDirectory(BaseJsonRpcApi):
    """
    Read a directory on the remote Kodi host

    Used for smart playlists: the playlists folder is a directory of ``.xsp`` files, and one
    ``.xsp`` path is itself a directory of the items its rules matched. ``media`` must be
    ``video`` or the host returns an empty list for a playlist.
    """

    method = 'Files.GetDirectory'

    def __init__(self, directory):
        super().__init__()
        self._directory = directory

    def get_params(self) -> Dict[str, Any]:
        return {
            'directory': self._directory,
            'media': 'video',
            'properties': DIRECTORY_PROPERTIES,
        }

    def get_files(self) -> List[Dict[str, Any]]:
        """
        The entries of this directory

        :raises: NoDataError when the host does not return a usable directory listing
        """
        try:
            return self.send_json_rpc()['result']['files'] or []
        except (KeyError, TypeError) as exc:
            raise NoDataError(
                f'Unable to read {self._directory} on the remote Kodi host') from exc


SET_DETAILS_API_MAP = {
    'movieid': SetMovieDetails,
    'episodeid': SetEpisodeDetails,
}


def update_playcount(item_id_param, item_id, playcount):
    api_class = SET_DETAILS_API_MAP[item_id_param]
    api = api_class()
    kwargs = {
        item_id_param: item_id,
        'playcount': playcount,
        'resume': {'position': 0.0, 'total': 0.0},
    }
    api.set_details(**kwargs)


def update_resume(item_id_param, item_id, position, total):
    api_class = SET_DETAILS_API_MAP[item_id_param]
    api = api_class()
    api.set_details(**{item_id_param: item_id, 'resume': {'position': position, 'total': total}})


def update_lastplayed_and_resume(item_id_param, item_id, playcount, resume, lastplayed):
    api_class = SET_DETAILS_API_MAP[item_id_param]
    api = api_class()
    api.set_details(**{
        item_id_param: item_id,
        'playcount': playcount,
        'lastplayed': lastplayed,
        'resume': resume,
    })
