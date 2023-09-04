from auth import open_tidal_session, open_spotify_session
from functools import partial
from multiprocessing import Pool, freeze_support
import requests
import sys
import spotipy
import tidalapi
from tidalapi_patch import set_tidal_playlist
import time
import traceback
import unicodedata
import yaml
import requests
import os
import wx.adv
import wx
import os
import datetime

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


def normalize(s):
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string):
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

def isrc_match(tidal_track, spotify_track):
    if "isrc" in spotify_track["external_ids"]:
        return tidal_track.isrc == spotify_track["external_ids"]["isrc"]
    return False

def duration_match(tidal_track, spotify_track, tolerance=2):
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

def name_match(tidal_track, spotify_track):
    def exclusion_rule(pattern, tidal_track, spotify_track):
        spotify_has_pattern = pattern in spotify_track['name'].lower()
        tidal_has_pattern = pattern in tidal_track.name.lower() or (not tidal_track.version is None and (pattern in tidal_track.version.lower()))
        return spotify_has_pattern != tidal_has_pattern

    # handle some edge cases
    if exclusion_rule("instrumental", tidal_track, spotify_track): return False
    if exclusion_rule("acapella", tidal_track, spotify_track): return False
    if exclusion_rule("remix", tidal_track, spotify_track): return False

    # the simplified version of the Spotify track name must be a substring of the Tidal track name
    # Try with both un-normalized and then normalized
    simple_spotify_track = simple(spotify_track['name'].lower()).split('feat.')[0].strip()
    return simple_spotify_track in tidal_track.name.lower() or normalize(simple_spotify_track) in normalize(tidal_track.name.lower())

def artist_match(tidal_track, spotify_track):
    def split_artist_name(artist):
       if '&' in artist:
           return artist.split('&')
       elif ',' in artist:
           return artist.split(',')
       else:
           return [artist]

    def get_tidal_artists(tidal_track, do_normalize=False):
        result = []
        for artist in tidal_track.artists:
            if do_normalize:
                artist_name = normalize(artist.name)
            else:
                artist_name = artist.name
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])

    def get_spotify_artists(spotify_track, do_normalize=False):
        result = []
        for artist in spotify_track['artists']:
            if do_normalize:
                artist_name = normalize(artist['name'])
            else:
                artist_name = artist['name']
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])
    # There must be at least one overlapping artist between the Tidal and Spotify track
    # Try with both un-normalized and then normalized
    if get_tidal_artists(tidal_track).intersection(get_spotify_artists(spotify_track)) != set():
        return True
    return get_tidal_artists(tidal_track, True).intersection(get_spotify_artists(spotify_track, True)) != set()

def match(tidal_track, spotify_track):
    return isrc_match(tidal_track, spotify_track) or (
        duration_match(tidal_track, spotify_track)
        and name_match(tidal_track, spotify_track)
        and artist_match(tidal_track, spotify_track)
    )


def tidal_search(spotify_track_and_cache, tidal_session):
    spotify_track, cached_tidal_track = spotify_track_and_cache
    if cached_tidal_track: return cached_tidal_track
    # search for album name and first album artist
    if 'album' in spotify_track and 'artists' in spotify_track['album'] and len(spotify_track['album']['artists']):
        album_result = tidal_session.search(simple(spotify_track['album']['name']) + " " + simple(spotify_track['album']['artists'][0]['name']), models=[tidalapi.album.Album])
        for album in album_result['albums']:
            album_tracks = album.tracks()
            if len(album_tracks) >= spotify_track['track_number']:
                track = album_tracks[spotify_track['track_number'] - 1]
                if match(track, spotify_track):
                    return track
    # if that fails then search for track name and first artist
    for track in tidal_session.search(simple(spotify_track['name']) + ' ' + simple(spotify_track['artists'][0]['name']), models=[tidalapi.media.Track])['tracks']:
        if match(track, spotify_track):
            return track

def get_tidal_playlists_dict(tidal_session):
    # a dictionary of name --> playlist
    tidal_playlists = tidal_session.user.playlists()
    output = {}
    for playlist in tidal_playlists:
        output[playlist.name] = playlist
    return output 

def repeat_on_request_error(function, *args, remaining=5, **kwargs):
    # utility to repeat calling the function up to 5 times if an exception is thrown
    try:
        return function(*args, **kwargs)
    except requests.exceptions.RequestException as e:
        if remaining:
            print(f"{str(e)} occurred, retrying {remaining} times")
        else:
            print(f"{str(e)} could not be recovered")

        if not e.response is None:
            print(f"Response message: {e.response.text}")
            print(f"Response headers: {e.response.headers}")

        if not remaining:
            print("Aborting sync")
            print(f"The following arguments were provided:\n\n {str(args)}")
            print(traceback.format_exc())
            sys.exit(1)
        sleep_schedule = {5: 1, 4:10, 3:60, 2:5*60, 1:10*60} # sleep variable length of time depending on retry number
        time.sleep(sleep_schedule.get(remaining, 1))
        return repeat_on_request_error(function, *args, remaining=remaining-1, **kwargs)

def _enumerate_wrapper(value_tuple, function, **kwargs):
    # just a wrapper which accepts a tuple from enumerate and returns the index back as the first argument
    index, value = value_tuple
    return (index, repeat_on_request_error(function, value, **kwargs))

def call_async_with_progress(function, values, description, num_processes, **kwargs):
    results = len(values)*[None]
    with Pool(processes=num_processes) as process_pool:
        for index, result in process_pool.imap_unordered(partial(_enumerate_wrapper, function=function, **kwargs),
                                  enumerate(values)):
            results[index] = result
    return results

def get_tracks_from_spotify_playlist(spotify_session, spotify_playlist):
    output = []
    results = spotify_session.playlist_tracks(
        spotify_playlist["id"],
        fields="next,items(track(name,album(name,artists),artists,track_number,duration_ms,id,external_ids(isrc)))",
    )
    while True:
        output.extend([r['track'] for r in results['items'] if r['track'] is not None])
        # move to the next page of results if there are still tracks remaining in the playlist
        if results['next']:
            results = spotify_session.next(results)
        else:
            return output

class TidalPlaylistCache:
    def __init__(self, playlist):
        self._data = playlist.tracks()

    def _search(self, spotify_track):
        ''' check if the given spotify track was already in the tidal playlist.'''
        results = []
        for tidal_track in self._data:
            if match(tidal_track, spotify_track):
                return tidal_track
        return None

    def search(self, spotify_session, spotify_playlist):
        ''' Add the cached tidal track where applicable to a list of spotify tracks '''
        results = []
        cache_hits = 0
        work_to_do = False
        spotify_tracks = get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
        for track in spotify_tracks:
            cached_track = self._search(track)
            if cached_track:
                results.append( (track, cached_track) )
                cache_hits += 1
            else:
                results.append( (track, None) )
        return (results, cache_hits)

def tidal_playlist_is_dirty(playlist, new_track_ids):
    old_tracks = playlist.tracks()
    if len(old_tracks) != len(new_track_ids):
        return True
    for i in range(len(old_tracks)):
        if old_tracks[i].id != new_track_ids[i]:
            return True
    return False

def sync_playlist(spotify_session, tidal_session, spotify_id, tidal_id, config):
    try:
        spotify_playlist = spotify_session.playlist(spotify_id)
    except spotipy.SpotifyException as e:
        print("Error getting Spotify playlist " + spotify_id + "make sure the playlist is yours and the ID is correct")
        #print(e)
        #results.append(None)
        return
    
    if tidal_id:
        # if a Tidal playlist was specified then look it up
        try:
            tidal_playlist = tidal_session.playlist(tidal_id)
        except Exception as e:
            print("Error getting Tidal playlist " + tidal_id)
            print(e)
            return
    else:
        # create a new Tidal playlist if required
        print(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
        tidal_playlist =  tidal_session.user.create_playlist(spotify_playlist['name'], spotify_playlist['description'])
    tidal_track_ids = []
    spotify_tracks, cache_hits = TidalPlaylistCache(tidal_playlist).search(spotify_session, spotify_playlist)
    if cache_hits == len(spotify_tracks):
        print("No new tracks to search in Spotify playlist '{}'".format(spotify_playlist['name']))
        return
    print ("Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(spotify_tracks) - cache_hits, len(spotify_tracks), spotify_playlist['name']))
    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(spotify_tracks) - cache_hits, len(spotify_tracks), spotify_playlist['name'])
    tidal_tracks = call_async_with_progress(tidal_search, spotify_tracks, task_description, config.get('subprocesses', 50), tidal_session=tidal_session)
    print ('Search done')
    for index, tidal_track in enumerate(tidal_tracks):
        spotify_track = spotify_tracks[index][0]
        if tidal_track:
            tidal_track_ids.append(tidal_track.id)
        else:
            print("Could not find track : {} - {}".format([a['name'] for a in spotify_track['artists']], spotify_track['name']))
    if tidal_playlist_is_dirty(tidal_playlist, tidal_track_ids):
        set_tidal_playlist(tidal_playlist, tidal_track_ids)
    else:
        print("No changes to write to Tidal playlist")

def sync_list(spotify_session, tidal_session, playlists, config):
  results = []
  for spotify_id, tidal_id in playlists:
    # sync the spotify playlist to tidal
    repeat_on_request_error(sync_playlist, spotify_session, tidal_session, spotify_id, tidal_id, config)
    results.append(tidal_id)
  return results

def pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists):
    if spotify_playlist['name'] in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_playlist['name']]
      return (spotify_playlist['id'], tidal_playlist.id)
    else:
      return (spotify_playlist['id'], None)
    


def sync(url):
    if url=='':
        print ("Missing ID!")
    else:
        with open("config.yml", 'r') as f:
            config = yaml.safe_load(f)
        spotify_session = open_spotify_session(config['spotify'])
        tidal_session = open_tidal_session()
        if not tidal_session.check_login():
            sys.exit("Could not connect to Tidal")
        try:
            spotify_playlist = spotify_session.playlist(url)
        except spotipy.SpotifyException as e:
            print("Error getting Spotify playlist \"" + url + "\"\nMake sure the playlist ID is correct.")
            print(e)
            return
        tidal_playlists = get_tidal_playlists_dict(tidal_session)
        tidal_playlist = pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
        sync_list(spotify_session, tidal_session, [tidal_playlist], config)
        

TRAY_TOOLTIP = 'Taskspydal' 
TRAY_ICON = 'icon.ico' 
def create_menu_item(menu, label, func):
    item = wx.MenuItem(menu, -1, label)
    menu.Bind(wx.EVT_MENU, func, id=item.GetId())
    menu.Append(item)
    return item

class TaskBarIcon(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        self.frame = frame
        super(TaskBarIcon, self).__init__()
        self.set_icon(TRAY_ICON)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_left_down)

    def CreatePopupMenu(self):
        menu = wx.Menu()
        create_menu_item(menu, 'Site', self.on_hello)
        menu.AppendSeparator()
        create_menu_item(menu, 'Exit', self.on_exit)
        return menu

    def set_icon(self, path):
        icon = wx.Icon(resource_path("icon.ico"))
        self.SetIcon(icon, TRAY_TOOLTIP)

    def on_left_down(self, event):      
        print ('Tray icon was left-clicked.')

    def on_hello(self, event):
        print ('Hello, world!')

    def on_exit(self, event):
        wx.CallAfter(self.Destroy)
        self.frame.Close()

class App(wx.App):
    def OnInit(self):
        frame=wx.Frame(None)
        self.SetTopWindow(frame)
        TaskBarIcon(frame)
        return True
def check_sync_needed():
    # Read the config file
    with open("config.yml", 'r') as f:
        config = yaml.safe_load(f)
    current_time = datetime.datetime.now()
    # Dictionary to store IDs that need syncing
    ids_to_sync = {}
    for id_value, id_data in config['schedule'].items():
        last_up_time = datetime.datetime.strptime(id_data['last_up'], '%d/%m/%Y %H:%M:%S')
        time_difference = current_time - last_up_time
        print(time_difference)
        if id_data['type'] == 'HOURLY':
            sync_interval = datetime.timedelta(hours=1)
        elif id_data['type'] == 'DAILY':
            sync_interval = datetime.timedelta(days=1)
        elif id_data['type'] == 'WEEKLY':
            sync_interval = datetime.timedelta(weeks=1)
        elif id_data['type'] == 'MONTHLY':
            sync_interval = datetime.timedelta(days=30) 

        if time_difference >= sync_interval:
            id_data['last_up'] = current_time.strftime('%d/%m/%Y %H:%M:%S')
            ids_to_sync[id_value] = id_value

    return ids_to_sync

def main():
    app = App(False)
    for id_value in check_sync_needed():
        sync(id_value)
    sys.exit(0)

if __name__ == "__main__":
    freeze_support()
    main()
    
    
