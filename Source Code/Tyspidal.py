
from auth import open_tidal_session, open_spotify_session
from functools import partial
from multiprocessing import Pool, freeze_support
import requests
import ctypes, sys
import spotipy
import tidalapi
from tidalapi_patch import set_tidal_playlist
import time
import traceback
import unicodedata
import yaml
import threading
import os
import webbrowser
from bs4 import BeautifulSoup
import urllib.request



import customtkinter

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


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
        with open('config.yml', 'r') as f:
            config = yaml.safe_load(f)
        spotify_session = open_spotify_session(config['spotify'])
        tidal_session = open_tidal_session()
        if not tidal_session.check_login():
            sys.exit("Could not connect to Tidal")
        try:
            spotify_playlist = spotify_session.playlist(url)
        except spotipy.SpotifyException as e:
            print("Error getting Spotify playlist \"" + url + "\"\nMake sure the playlist is yours and the ID is correct.")
            #print(e)
            #results.append(None)
            return
        tidal_playlists = get_tidal_playlists_dict(tidal_session)
        tidal_playlist = pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
        sync_list(spotify_session, tidal_session, [tidal_playlist], config)





class StdoutRedirector:
    def __init__(self, callback):
        self.callback = callback

    def write(self, text):
        self.callback(text)
    def flush(self):
        pass 




class App(customtkinter.CTk):
    def __init__(self):
        super().__init__()

        self.title("Tyspidal")
        self.geometry("700x450")

        # set grid layout 1x2
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        def sync_and_update_output(url):
            def update_output(text):
                output_text.insert("end", text)
                output_text.see("end")

            sys.stdout = StdoutRedirector(update_output)
            print ("Initializing...")
            sync(url)
            print ("Done!")
            sys.stdout = sys.__stdout__  # Restore the original sys.stdout

        def button_sync():
            url = entry_1.get()
            t = threading.Thread(target=sync_and_update_output, args=(url,))
            t.start()

        # create navigation frame
        self.navigation_frame = customtkinter.CTkFrame(self, corner_radius=0)
        self.navigation_frame.grid(row=0, column=0, sticky="nsew")
        self.navigation_frame.grid_rowconfigure(4, weight=1)

        
        self.home_button = customtkinter.CTkButton(self.navigation_frame, corner_radius=0, height=40, border_spacing=10, text="Profile",
                                                   fg_color="transparent", text_color=("gray10", "gray90"), hover_color=("gray70", "gray30"), anchor="w", command=self.home_button_event)
        self.home_button.grid(row=2, column=0, sticky="ew")

        self.frame_2_button = customtkinter.CTkButton(self.navigation_frame, corner_radius=0, height=40, border_spacing=10, text="Sync (Spotify/Tidal)",
                                                      fg_color="transparent", text_color=("gray10", "gray90"), hover_color=("gray70", "gray30"), anchor="w", command=self.frame_2_button_event)
        self.frame_2_button.grid(row=3, column=0, sticky="ew")

        self.frame_3_button = customtkinter.CTkButton(self.navigation_frame, corner_radius=0, height=40, border_spacing=10, text="About/Credits",
                                                      fg_color="transparent", text_color=("gray10", "gray90"), hover_color=("gray70", "gray30"), anchor="w", command=self.frame_3_button_event)
        self.frame_3_button.grid(row=5, column=0, sticky="ew")

        self.appearance_mode_menu = customtkinter.CTkOptionMenu(self.navigation_frame, values=["Light", "Dark", "System"],
                                                                command=self.change_appearance_mode_event)
        self.appearance_mode_menu.grid(row=7, column=0, padx=20, pady=20, sticky="s")
        self.appearance_mode_menu.set("Dark")
        
        # create home frame
        def callback(url):
            webbrowser.open_new(url)
        self.home_frame = customtkinter.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.home_frame.grid_columnconfigure(0, weight=1)
        with open('config.yml', 'r') as f:
            config = yaml.safe_load(f)
        def button_update():
            fname = "config.yml"
            config['spotify']['client_id']=entry_2.get()
            config['spotify']['client_secret']=entry_3.get()
            config['spotify']['username']=entry_4.get()
            config['spotify']['redirect_uri']=entry_5.get()
            with open(fname, 'w') as yml_file:
                yml_file.write(yaml.dump(config, default_flow_style=False))
        label_1 = customtkinter.CTkLabel(master=self.home_frame, justify=customtkinter.LEFT, text="Welcome!")
        label_1.pack(pady=0, padx=0)
        label_1 = customtkinter.CTkLabel(master=self.home_frame, justify=customtkinter.LEFT, text="Client ID")
        label_1.pack(pady=0, padx=0)
        entry_2 = customtkinter.CTkEntry(master=self.home_frame, placeholder_text=config['spotify']['client_id'])
        entry_2.pack(pady=0, padx=0)
        entry_2.insert(0,config['spotify']['client_id'])
        label_1 = customtkinter.CTkLabel(master=self.home_frame, justify=customtkinter.LEFT, text="Client Secret")
        label_1.pack(pady=0, padx=0)
        entry_3 = customtkinter.CTkEntry(master=self.home_frame, placeholder_text=config['spotify']['client_secret'])
        entry_3.pack(pady=0, padx=0)
        entry_3.insert(0,config['spotify']['client_secret'])
        label_1 = customtkinter.CTkLabel(master=self.home_frame, justify=customtkinter.LEFT, text="Username")
        label_1.pack(pady=0, padx=0)
        entry_4 = customtkinter.CTkEntry(master=self.home_frame, placeholder_text=config['spotify']['username'])
        entry_4.pack(pady=0, padx=0)
        entry_4.insert(0,config['spotify']['username'])
        label_1 = customtkinter.CTkLabel(master=self.home_frame, justify=customtkinter.LEFT, text="Redirect_uri")
        label_1.pack(pady=0, padx=0)
        entry_5 = customtkinter.CTkEntry(master=self.home_frame, placeholder_text=config['spotify']['redirect_uri'])
        entry_5.pack(pady=0, padx=0)
        entry_5.insert(0,config['spotify']['redirect_uri'])

        button_1 = customtkinter.CTkButton(master=self.home_frame, command=button_update,text="Update")
        button_1.pack(pady=20, padx=10)

        label_1 = customtkinter.CTkLabel(master=self.home_frame, justify=customtkinter.CENTER, text="The Client ID and Client Secret are needed for authorization \nTo get yours, go to your spotify dashbord and click on the Create an App button")
        label_1.pack(pady=0, padx=0)
        label= customtkinter.CTkLabel(master=self.home_frame,justify=customtkinter.CENTER, text="Open your Spotify dashboard", cursor="hand2",text_color="blue")
        label.pack(pady=0, padx=0)
        label.bind("<Button-1>", lambda e: callback("https://developer.spotify.com/dashboard"))
        


        # create second frame
        self.second_frame = customtkinter.CTkFrame(self, corner_radius=0, fg_color="transparent")
        label_1 = customtkinter.CTkLabel(master=self.second_frame, justify=customtkinter.LEFT, text="Spotify playlist ID")
        label_1.pack(pady=0, padx=0)
        entry_1 = customtkinter.CTkEntry(master=self.second_frame, placeholder_text="ID")
        entry_1.pack(pady=0, padx=0)

        button_2 = customtkinter.CTkButton(master=self.second_frame, command=button_sync,text="Sync")
        button_2.pack(pady=20, padx=10)

        console_frame = customtkinter.CTkFrame(master=self.second_frame)
        console_frame.pack(fill="both", expand=True)
        output_text  = customtkinter.CTkTextbox(master=console_frame)
        output_text.pack(pady=10, padx=10, fill="both", expand=True)

        

        # create third frame
        self.third_frame = customtkinter.CTkFrame(self, corner_radius=0, fg_color="transparent")
        label_1 = customtkinter.CTkLabel(master=self.third_frame, justify=customtkinter.CENTER, text="Thanks to the creators of the following repositories:")
        label_1.pack(pady=0, padx=0)
        label= customtkinter.CTkLabel(master=self.third_frame,justify=customtkinter.CENTER, text="Custom Tkinter", cursor="hand2",text_color="blue")
        label.pack(pady=0, padx=0)
        label.bind("<Button-1>", lambda e: callback("https://github.com/TomSchimansky/CustomTkinter"))
        label= customtkinter.CTkLabel(master=self.third_frame,justify=customtkinter.CENTER, text="Spotify to Tidal", cursor="hand2",text_color="blue")
        label.pack(pady=0, padx=0)
        label.bind("<Button-1>", lambda e: callback("https://github.com/timrae/spotify_to_tidal"))
        def connect(host='http://google.com'):
            try:
                urllib.request.urlopen(host) #Python 3.x
                return True
            except:
                return False
        if connect() :
            url = "https://ivory-britney-30.tiiny.site/"
            response = requests.get(url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                text = soup.get_text()
                label_1 = customtkinter.CTkLabel(master=self.third_frame, justify=customtkinter.CENTER, text=text)
                label_1.pack(pady=0, padx=0)
                self.status = customtkinter.CTkLabel(self.navigation_frame,justify=customtkinter.CENTER, corner_radius=0, height=40, text="      ONLINE",
                                                   fg_color="green", text_color=("gray10", "gray90"), anchor="w")
                self.status.grid(row=6, column=0, sticky="ew")
        else: 
            self.status = customtkinter.CTkLabel(self.navigation_frame,justify=customtkinter.CENTER, corner_radius=0, height=40, text="OFFLINE \n Go ONLINE and relaunch Tyspidal ",
                                                   fg_color="red", text_color=("gray10", "gray90"), anchor="w")
            self.status.grid(row=6, column=0, sticky="ew")

        


        # select default frame
        self.select_frame_by_name("home")

    def select_frame_by_name(self, name):
        # set button color for selected button
        self.home_button.configure(fg_color=("gray75", "gray25") if name == "home" else "transparent")
        self.frame_2_button.configure(fg_color=("gray75", "gray25") if name == "frame_2" else "transparent")
        self.frame_3_button.configure(fg_color=("gray75", "gray25") if name == "frame_3" else "transparent")

        # show selected frame
        if name == "home":
            self.home_frame.grid(row=0, column=1, sticky="nsew")
        else:
            self.home_frame.grid_forget()
        if name == "frame_2":
            self.second_frame.grid(row=0, column=1, sticky="nsew")
        else:
            self.second_frame.grid_forget()
        if name == "frame_3":
            self.third_frame.grid(row=0, column=1, sticky="nsew")
        else:
            self.third_frame.grid_forget()

    def home_button_event(self):
        self.select_frame_by_name("home")

    def frame_2_button_event(self):
        self.select_frame_by_name("frame_2")

    def frame_3_button_event(self):
        self.select_frame_by_name("frame_3")

    def change_appearance_mode_event(self, new_appearance_mode):
        customtkinter.set_appearance_mode(new_appearance_mode)


if __name__ == "__main__":
    freeze_support() 
    if is_admin():
        config_data = {
        'spotify': {
            'client_id': '',
            'client_secret': '',
            'redirect_uri': 'http://localhost:8888/callback',
            'username': ''
        }
        }

        config_file_path = 'config.yml'

        if not os.path.exists(config_file_path):
            with open(config_file_path, 'w') as config_file:
                yaml.dump(config_data, config_file, default_flow_style=False)
                print(f"Created {config_file_path} with default values.")
        else:
            print(f"{config_file_path} already exists.")
        app = App()
        app.mainloop()
    else:
    # Re-run the program with admin rights
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)