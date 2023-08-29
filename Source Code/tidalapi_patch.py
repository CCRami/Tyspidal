
def _remove_indices_from_playlist(playlist, indices):
    headers = {'If-None-Match': playlist._etag}
    index_string = ",".join(map(str, indices))
    playlist.requests.request('DELETE', (playlist._base_url + '/items/%s') % (playlist.id, index_string), headers=headers)
    playlist._reparse()

def clear_tidal_playlist(playlist, chunk_size=20):
    while playlist.num_tracks:
        indices = range(min(playlist.num_tracks, chunk_size))
        _remove_indices_from_playlist(playlist, indices)

def add_multiple_tracks_to_playlist(playlist, track_ids, chunk_size=20):
    offset = 0
    while offset < len(track_ids):
        count = min(chunk_size, len(track_ids) - offset)
        playlist.add(track_ids[offset:offset+chunk_size])
        offset += count
    

def set_tidal_playlist(playlist, track_ids):
    print("Erasing existing tracks from Tidal playlist...")
    clear_tidal_playlist(playlist)
    print("Adding new tracks to Tidal playlist...")
    add_multiple_tracks_to_playlist(playlist, track_ids)
