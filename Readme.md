# External Kodi Videolibrary Client

[![Check addon](https://github.com/romanvm/kodi.external.library/actions/workflows/check-addon.yml/badge.svg)](https://github.com/romanvm/kodi.external.library/actions/workflows/check-addon.yml)

This addon allows to browse and play videofiles from a medialibrary on a remote Kodi instance with syncing playback status.

## Motivation
Low-powered mini PCs are really nifty as media servers, and with the help of streaming technologies like Parsec/Moonlight, they can run a perfectly fine Kodi server and stream to other clients on the LAN... up to a certain point, before the device gives up and suffers from thermal throttling and overheating, despite dedicated external cooling measures. The solution is ez, just buy better hardware! Then you get slapped by how much RAM and storage costs nowadays. Ouch!

Just when I thought I'd hit a wall, I stumbled upon Kodi's external library add-on, which opened up a more practical way to browse and stream media across devices without relying so heavily on the server's hardware.

Building on the original author's work, this fork adds features developed entirely through AI-assisted “vibe coding,” alongside smaller refinements aimed at making browsing faster and the experience feel as native to Kodi as possible, especially on lower-powered devices like the Fire TV Stick 4K. The library listing and playback workflow have also been tested with Google Drive and OneDrive add-ons for cloud-hosted media.

## Features

### Fast(er) Browsing
- **Fast(er) local browsing** — Browse Movies and TV Shows from a local library copy, reducing repeated network requests to the remote Kodi host.
- **Recently Added TV Shows** — Added the menu to quickly access recently added TV shows from a dedicated menu entry.
- **On-demand library building** — Build the local library copy directly from the Movies or TV Shows menu without opening the list (for case for 5000 library items or something)

### Smart Playlists
- **Remote Smart Playlists** — Browse and open Smart Playlists from your remote Kodi library.
- **Pin to root menu** — Pin frequently used playlists to the root menu for quick access.
- **Configurable pagination** — Browse large playlists in manageable pages, customize the page size, or disable pagination entirely.
- **Rich playlist details** — Display posters, plots, and other metadata for playlist entries using the local library copy, with an option to use a lighter view.

### Flexible Interface
- **Native Kodi library views** — Use Kodi's familiar library layouts, or switch to the generic file view.
- **Sorting and metadata** — Access supported sorting options and full item information when browsing your library.

### Watch Tracking
- **Automatic watch-state synchronization** — Update watched status and last-played information on the remote Kodi host after playback, including when rewatching previously watched episodes.
- **No forced list refresh after playback** — Lists are intentionally left unchanged when playback ends, avoiding unnecessary delays. Updated watch information appears the next time the list is opened.


## Credits

The addon icon was borrowed from [Aeon Nox 5: SiLVO](https://github.com/MikeSiLVO/Aeon-Nox-SiLVO) skin.


## Known Issue or unresolved and(or) out of scope
- ** Network strm based Items in library view are missing metadata or read as "Startup" before the client cached or download additional metadata? 
- ** Building views from TMDB Helper service was considered at one point and was rejected, might either be too complicated or not feasible directly from this side of plugin.
- ** Some alternative play method was used to use plugin based url instead of direct vfs for gdrive/onedrive, usually either resulted in broken playback, or was at one point working but broke the resume point insertion feature. 

## License
This project retains the GNU General Public License v3.0 (GPL-3.0). See LICENSE for the full license text.
[GPL v.3](https://www.gnu.org/licenses/gpl-3.0.html)
