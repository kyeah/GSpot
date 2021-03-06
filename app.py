from gevent import monkey
monkey.patch_all()

import config
import json
import logging
import re
import spotipy.util as util
import sys

from getpass import getpass
from gevent.pool import Group
from gmusicapi import Mobileclient
from spotipy import Spotify

formatter = logging.Formatter("%(asctime)s;%(levelname)s;%(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")

handler = logging.FileHandler('.log')
handler.setFormatter(formatter)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
log.addHandler(handler)

re_title = [
    re.compile("(.*) \(feat\. (.*)\)"),
    re.compile("(.*) featuring (.*)")
]
re_artist = [
    re.compile("(.*) & (.*)"),
    re.compile("(.*) ft\. (.*)"),
    re.compile("(.*) vs (.*)"),
    re.compile("(.*) - (.*)"),
    re.compile("(.*) + (.*)"),
    re.compile("(.*) / (.*)")
]

def extract_track_matches(name, artist):
    """ Find base titles and collaboration artists """
    names, artists = [], []
    names.append(name)
    artists.append(artist)

    # Strip from title
    for regex in re_title:
        res = regex.search(name)
        if res:
            names.append(res.group(1))
            artists.append(res.group(2))

    # Strip from artist
    for regex in re_artist:
        res = regex.search(artist)
        if res:
            artists.append(res.group(1))
            artists.append(res.group(2))

    return names, artists

def chunker(seq, size):
    return (seq[pos:pos + size] for pos in range(0, len(seq), size))

def get_google_library(g):
    dic = {}
    for song in g.get_all_songs():
        dic[song['id']] = song
    return dic

def login_google():
    """ Log into Google and retrieve user library and playlists """

    g = Mobileclient()
    logged_in = g.login(config.auth['GOOGLE_EMAIL'], 
                        config.auth['GOOGLE_PASSWORD'],
                        Mobileclient.FROM_MAC_ADDRESS)

    if not g.is_authenticated():
        log.error("Invalid Google email/password; exiting.")
        sys.exit(1)

    log.info("Retrieving Google Music playlists")
    g.playlists = g.get_all_user_playlist_contents()

    log.info("Retrieving Google Music library")
    g.library = get_google_library(g)

    return g

def login_spotify():
    """ Log into Spotify and retrieve user playlists """

    scope = 'playlist-modify-public playlist-modify-private'
    token = util.prompt_for_user_token(config.auth['SPOTIFY_EMAIL'], scope)

    if not token:
        log.error("Invalid Spotify token; exiting.")
        sys.exit(1)

    s = Spotify(auth=token)
    s.username = config.auth['SPOTIFY_USERNAME']

    playlists = s.user_playlists(s.username)['items']
    s.playlists = {}

    for sl in playlists:
        s.playlists[sl['name']] = sl

    return s

def transfer_playlist(g, s, playlist):
    """ Synchronize Google Music playlist to Spotify """
    
    # Retrieve or create associated Spotify playlist
    name = playlist['name']
    spotlist = s.playlists.get(name, None) \
               or s.user_playlist_create(s.username, name)

    action = "Updating" if name in s.playlists else "Creating"
    log.info("%s playlist '%s'" % (action, name))

    # Find Spotify track IDs for each new song
    tasks = [(g, s, track) for track in playlist['tracks']
             if float(track['creationTimestamp']) > float(config.since)]

    pool = Group()
    results = pool.map(lambda args: find_track_id(*args), tasks)

    track_ids, not_found = [], []
    for (ok, track_info) in results:
        (track_ids if ok else not_found).append(track_info)

    for nf in not_found:
        log.warning("Track not found for '%s': '%s'" % (name, nf))

    # Filter for songs not yet synchronized to Spotify
    spotlist_info = s.user_playlist(s.username, playlist_id=spotlist['id'], fields="tracks,next")

    tracks = spotlist_info['tracks']
    spotlist_tracks = [x['track']['id'] for x in tracks['items']]
    while tracks['next']:
        tracks = s.next(tracks)
        spotlist_tracks += [x['track']['id'] for x in tracks['items']]

    new_ids = [x for x in track_ids if x not in spotlist_tracks]

    # Add new songs!!!
    log.info("Adding %d new tracks to '%s'!!!!!!" % (len(new_ids), name))
    for group in chunker(new_ids, 100):
        s.user_playlist_add_tracks(s.username, spotlist['id'], group)

def find_track_id(g, s, track):
    """ Find Spotify ID for associated Google Music track """

    if "title" not in track:
        tid = track['trackId']
        track = g.get_track_info(tid) if tid.startswith('T') \
                else g.library[tid]

    name, artist = track['title'], track['artist']

    results = None
    res = s.search('track:%s artist:%s' % (name, artist))
    try:
        results = res['tracks']['items']
    except:
        log.error('Results are None: %s, %s' % (name, artist))

    if results:
        return (True, results[0]['id'])
    else:
        # Spotify and Google Music handle collaborations differently :(
        names, artists = extract_track_matches(name, artist)
        for name in names:
            for artist in artists:
                results = None
                res = s.search('track:%s artist:%s' % (name, artist))
                try:
                    results = res['tracks']['items']
                except:
                    log.error('Results are None: %s, %s' % (name, artist))

                if results:
                    return (True, results[0]['id'])

        return (False, "%s - %s" % (name, artist))

def main():

    g = login_google()
    s = login_spotify()

    # Filter playlists by config and last sync
    g.playlists = [p for p in g.playlists
                   if (not config.playlists or 
                       p['name'] in config.playlists)
                   and p['name'] not in config.exclude
                   and float(p['lastModifiedTimestamp']) > float(config.since)]

    # Transfer playlists
    tasks = [(g, s, playlist) for playlist in g.playlists]
    pool = Group()
    pool.map(lambda args: transfer_playlist(*args), tasks)

if __name__ == '__main__':
    main()
