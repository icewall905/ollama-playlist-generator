from flask import Blueprint, render_template, request, jsonify, Response, current_app
import requests
import json
import configparser
import os
import datetime
import time
import re
import random
import xml.etree.ElementTree as ET
from urllib.parse import quote
import logging
from logging.handlers import RotatingFileHandler

# --- Logger Setup ---
LOG_DIR = 'logs'  # This will be relative to the project root (TuneForge/)
# Ensure the log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

app_file_logger = logging.getLogger('TuneForgeApp')
app_file_logger.setLevel(logging.DEBUG)  # Process all levels from debug_log

# Check if a similar handler already exists to prevent duplicates during reloads
handler_exists = any(
    isinstance(h, RotatingFileHandler) and \
    getattr(h, 'baseFilename', '') == os.path.abspath(os.path.join(LOG_DIR, 'tuneforge_app.log'))
    for h in app_file_logger.handlers
)

if not handler_exists:
    log_file_path = os.path.join(LOG_DIR, 'tuneforge_app.log')
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=5*1024*1024,  # 5 MB
        backupCount=5,
        encoding='utf-8'
    )
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    app_file_logger.addHandler(file_handler)
    # If you also want these logs in the console (in addition to the file), uncomment below
    # console_handler = logging.StreamHandler()
    # console_handler.setFormatter(formatter)
    # app_file_logger.addHandler(console_handler)

# --- Global Debug Flags ---
DEBUG_ENABLED = True  # Master debug switch
DEBUG_OLLAMA_RESPONSE = False  # Specific for Ollama response logging, can also be set in config.ini

main_bp = Blueprint('main', __name__)

CONFIG_FILE = 'config.ini'
HISTORY_FILE = 'playlist_history.json'

# --- Config Functions ---
def load_config():
    config = configparser.ConfigParser()
    # Preserve case for keys
    config.optionxform = lambda optionstr: optionstr  # Preserve case
    if not os.path.exists(CONFIG_FILE):
        # Fallback to example if main config doesn't exist, or create empty
        example_config_file = CONFIG_FILE + '.example'
        if os.path.exists(example_config_file):
            debug_log(f"{CONFIG_FILE} not found, loading {example_config_file}", "WARN")
            config.read(example_config_file)
            # Optionally save it as config.ini now
            # with open(CONFIG_FILE, 'w') as f:
            #     config.write(f)
        else:
            debug_log(f"{CONFIG_FILE} and {example_config_file} not found. Using empty config.", "WARN")
            # Setup default sections if needed, or let it be empty
            config.add_section('OLLAMA')
            config.add_section('APP')
            config.add_section('NAVIDROME')
            config.add_section('PLEX')
    else:
        config.read(CONFIG_FILE)
    return config

def get_config_value(section, key, default=None):
    config = load_config()
    if config.has_section(section):
        # config.optionxform ensures keys are case-sensitive as read
        # Direct case-sensitive access after optionxform is set:
        if key in config[section]:
            return config[section][key]
    return default

def save_config(data_dict):
    config = configparser.ConfigParser()
    config.optionxform = lambda optionstr: optionstr # Preserve case for keys when writing
    for section, options in data_dict.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in options.items():
            config.set(section, key, str(value))
    try:
        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
        debug_log(f"Configuration saved to {CONFIG_FILE}", "INFO")
    except Exception as e:
        debug_log(f"Error writing configuration to {CONFIG_FILE}: {e}", "ERROR", True)
        raise # Re-raise to inform the caller

# --- Playlist History Functions ---
def load_playlist_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r') as f:
            # Handle empty file
            content = f.read()
            if not content:
                return []
            history_data = json.loads(content)
            if isinstance(history_data, dict): # Handles case where a single playlist might have been saved directly
                return [history_data]
            elif not isinstance(history_data, list): # Handles malformed file (should be list)
                 debug_log(f"Playlist history file {HISTORY_FILE} does not contain a list. Resetting.", "WARN", True)
                 return []
            return history_data
    except json.JSONDecodeError:
        debug_log(f"Error decoding JSON from {HISTORY_FILE}. File might be corrupted or empty.", "ERROR", True)
        return [] # Return empty list on error
    except Exception as e:
        debug_log(f"An error occurred while loading playlist history: {e}", "ERROR", True)
        return []

def save_playlist_history(history_data):
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history_data, f, indent=4)
        debug_log(f"Playlist history saved to {HISTORY_FILE}", "INFO")
    except Exception as e:
        debug_log(f"Error saving playlist history to {HISTORY_FILE}: {e}", "ERROR", True)

# --- Debug Logging ---
_debug_flags_printed = False # Module-level flag to print debug status only once

def debug_log(message, level="INFO", force=False):
    global DEBUG_ENABLED, _debug_flags_printed
    # Check if debug is enabled in config
    debug_from_config_str = get_config_value('APP', 'Debug', 'yes') 
    debug_from_config = debug_from_config_str.lower() in ('yes', 'true', '1') if isinstance(debug_from_config_str, str) else False

    # Print status once
    if not _debug_flags_printed:
        print(f"[TuneForge Logger Init] DEBUG_ENABLED: {DEBUG_ENABLED}, Config 'APP','Debug': '{debug_from_config_str}', Parsed debug_from_config: {debug_from_config}")
        # Add an immediate test DEBUG message to verify logging works at that level
        app_file_logger.debug("This is a test DEBUG message during logger initialization")
        app_file_logger.info("This is a test INFO message during logger initialization")
        _debug_flags_printed = True
    
    if force or (DEBUG_ENABLED and debug_from_config):
        level_upper = level.upper()
        if level_upper == "DEBUG":
            app_file_logger.debug(message)
        elif level_upper == "INFO":
            app_file_logger.info(message)
        elif level_upper == "WARNING" or level_upper == "WARN":
            app_file_logger.warning(message)
        elif level_upper == "ERROR":
            app_file_logger.error(message)
        elif level_upper == "CRITICAL":
            app_file_logger.critical(message)
        else:
            # For unknown levels, log as INFO with the original level string in the message
            app_file_logger.info(f"[{level}] {message}")

# --- Ollama Interaction ---
def generate_tracks_with_ollama(ollama_url, ollama_model, prompt, num_songs, attempt_num=0, previously_suggested_tracks=None):
    global DEBUG_OLLAMA_RESPONSE # Global flag for verbose Ollama response logging
    # Configurable flag for verbose Ollama response logging
    config_debug_ollama = get_config_value('OLLAMA', 'DebugOllamaResponse', 'no').lower() in ('yes', 'true', '1')


    debug_log(f"Ollama: Attempting to generate {num_songs} tracks. Attempt: {attempt_num + 1}", "INFO")

    likes = get_config_value('APP', 'Likes', '')
    dislikes = get_config_value('APP', 'Dislikes', '')
    favorite_artists = get_config_value('APP', 'FavoriteArtists', '')
    # context_window = int(get_config_value('OLLAMA', 'ContextWindow', '2048')) # Not directly used here

    context_str = ""
    if previously_suggested_tracks and attempt_num > 0:
        recent_suggestions_for_prompt = []
        seen_in_context = set()
        # Look at last N suggestions (e.g., 50) to avoid overly long context
        for track in reversed(previously_suggested_tracks[-50:]): 
            track_key = (track.get("title", "").lower(), track.get("artist", "").lower())
            if track.get("title") and track.get("artist") and track_key not in seen_in_context:
                recent_suggestions_for_prompt.append(f"- '{track['title']}' by '{track['artist']}'")
                seen_in_context.add(track_key)
            if len(recent_suggestions_for_prompt) >= 15: # Limit context to ~15 distinct tracks
                break
        if recent_suggestions_for_prompt:
            context_str = "\\n\\nTo avoid repetition, DO NOT suggest any of the following tracks again:\\n" + "\\n".join(reversed(recent_suggestions_for_prompt))

    retry_guidance = ""
    if attempt_num > 0:
        retry_guidance = (
            "\\n\\nIMPORTANT: Your previous suggestions might have included repeats, undesirable versions, or didn't match well. "
            "Please provide COMPLETELY DIFFERENT suggestions this time. "
            "Focus on well-known, studio-recorded songs. Strongly AVOID live versions, instrumentals, karaoke, covers, remixes, demos, and edits unless the prompt specifically asks for them. "
            "Ensure variety in your new suggestions."
        )

    full_prompt = (
        f"You are a helpful music expert. Generate a list of exactly {num_songs} unique songs based on the following prompt: '{prompt}'.\\n"
        f"User Likes: {likes}\\nUser Dislikes: {dislikes}\\nUser Favorite Artists: {favorite_artists}\\n"
        f"Format each song strictly as 'Title - Artist - Album'. If an album is not applicable or known, use 'Unknown Album'.\\n"
        f"Each song must be on a new line. Do not include numbering, introductory/closing remarks, or any other text, just the songs in the specified format."
        f"{context_str}"
        f"{retry_guidance}"
    )

    # debug_log(f"Ollama full prompt:\\n{full_prompt}", "DEBUG") # Can be very verbose

    payload = {
        "model": ollama_model,
        "prompt": full_prompt,
        "stream": False,
        "options": {
            "temperature": float(get_config_value('OLLAMA', 'Temperature', '0.7')),
            "top_p": float(get_config_value('OLLAMA', 'TopP', '0.9')),
            "num_ctx": int(get_config_value('OLLAMA', 'ContextWindow', '2048')), # Max context window
            # "seed": random.randint(0, 2**32 -1) # For more deterministic results if needed for testing
        }
    }
    
    try:
        response = requests.post(f"{ollama_url.rstrip('/')}/api/generate", json=payload, timeout=120) # Increased timeout
        response.raise_for_status()
        response_data = response.json()

        if DEBUG_OLLAMA_RESPONSE or config_debug_ollama:
            debug_log(f"Ollama raw response JSON: {json.dumps(response_data)}", "DEBUG", True)

        content = response_data.get("response", "").strip()
        if not content:
            debug_log("Ollama response content is empty.", "WARN", True)
            return []

        tracks = []
        lines = content.split('\\n')
        for line in lines:
            line = line.strip()
            if not line: continue
            parts = line.split(' - ')
            if len(parts) >= 2:
                title = parts[0].strip().strip('\"\'')
                artist = parts[1].strip().strip('\"\'')
                album = parts[2].strip().strip('\"\'') if len(parts) >= 3 else "Unknown Album"
                if title and artist:
                    tracks.append({"title": title, "artist": artist, "album": album})
            else:
                debug_log(f"Ollama: Could not parse line: '{line}'", "WARN")
        
        debug_log(f"Ollama generated {len(tracks)} tracks from response.", "INFO")
        return tracks

    except requests.exceptions.Timeout:
        debug_log(f"Error calling Ollama: Timeout after 120 seconds.", "ERROR", True)
        return []
    except requests.exceptions.RequestException as e:
        debug_log(f"Error calling Ollama: {e}", "ERROR", True)
        return []
    except json.JSONDecodeError as e:
        debug_log(f"Error decoding Ollama JSON response: {e}. Response text: {response.text[:200] if 'response' in locals() else 'N/A'}", "ERROR", True)
        return []
    except Exception as e:
        debug_log(f"Unexpected error in generate_tracks_with_ollama: {e}", "ERROR", True)
        return []

# --- Navidrome Functions ---
def test_navidrome_connection(navidrome_url, username, password):
    result = {'success': False, 'error': None, 'details': {}, 'server_info': None}
    if not all([navidrome_url, username, password]):
        result['error'] = "Navidrome URL, Username, or Password missing."
        return result
    
    base_url = navidrome_url.rstrip('/')
    if '/rest' not in base_url: base_url = f"{base_url}/rest"
    result['details']['final_url'] = base_url
    
    ping_url = f"{base_url}/ping.view"
    params = {'u': username, 'p': password, 'v': '1.16.1', 'c': 'TuneForge', 'f': 'json'}
    
    try:
        ping_response = requests.get(ping_url, params=params, timeout=10)
        result['details']['ping_status_code'] = ping_response.status_code
        ping_response.raise_for_status() # Check for HTTP errors first
        
        ping_data = ping_response.json()
        result['details']['ping_response'] = ping_data
        
        if ping_data.get('subsonic-response', {}).get('status') == 'ok':
            result['success'] = True
            # Try to get server version info
            system_url = f"{base_url}/getSystemInfo.view"
            system_response = requests.get(system_url, params=params, timeout=10)
            if system_response.status_code == 200:
                system_data = system_response.json()
                if system_data.get('subsonic-response', {}).get('status') == 'ok':
                    result['server_info'] = system_data.get('subsonic-response', {}).get('systemInfo', {})
        else:
            error = ping_data.get('subsonic-response', {}).get('error', {})
            result['error'] = f"API Error: {error.get('message')} (code {error.get('code')})"
            
    except requests.exceptions.HTTPError as e:
        result['error'] = f"HTTP Error: {e.response.status_code} - {e.response.reason}. URL: {ping_url}"
        try: result['details']['ping_response_text'] = e.response.text[:500]
        except: pass
    except requests.exceptions.ConnectionError:
        result['error'] = f"Connection error - could not connect to Navidrome server at {navidrome_url}"
    except requests.exceptions.Timeout:
        result['error'] = "Connection timed out"
    except json.JSONDecodeError:
        result['error'] = "Could not parse JSON response from server"
        result['details']['ping_response_text'] = ping_response.text[:500] if 'ping_response' in locals() else "N/A"
    except requests.exceptions.RequestException as e:
        result['error'] = f"Request error: {str(e)}"
    return result

def search_track_in_navidrome(query, navidrome_url, username, password):
    if not navidrome_url: return []
    base_url = navidrome_url.rstrip('/')
    if '/rest' not in base_url: base_url = f"{base_url}/rest"
    
    url = f"{base_url}/search3.view"
    params = {
        'u': username, 'p': password, 'v': '1.16.1', 'c': 'TuneForge', 'f': 'json',
        'query': query, 'songCount': 20, 'artistCount': 0, 'albumCount': 0
    }
    # debug_log(f"Navidrome: Searching for query: '{query}'", "DEBUG")
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if data.get('subsonic-response', {}).get('status') == 'ok':
            songs = data.get('subsonic-response', {}).get('searchResult3', {}).get('song', [])
            # debug_log(f"Navidrome: Found {len(songs)} songs for query '{query}'", "DEBUG")
            tracks = []
            for song in songs:
                tracks.append({
                    'id': song.get('id'), 'title': song.get('title', 'Unknown Title'),
                    'artist': song.get('artist', 'Unknown Artist'), 'album': song.get('album', 'Unknown Album'),
                    'source': 'navidrome'
                })
            return tracks
        else:
            # error_message = data.get('subsonic-response', {}).get('error', {}).get('message')
            # debug_log(f"Navidrome: Error searching: {error_message}", "WARN")
            return []
    except requests.exceptions.RequestException: # Catches HTTPError, Timeout, ConnectionError
        # debug_log(f"Navidrome: Request error searching: {e}", "WARN")
        return []
    except json.JSONDecodeError:
        # debug_log(f"Navidrome: Error decoding search JSON response: {e}", "WARN")
        return []

def create_playlist_in_navidrome(navidrome_url, username, password, playlist_name, track_ids):
    if not navidrome_url:
        debug_log("Navidrome URL not configured, cannot create playlist.", "ERROR", True)
        return None
    base_url = navidrome_url.rstrip('/')
    if '/rest' not in base_url: base_url = f"{base_url}/rest"
    
    url = f"{base_url}/createPlaylist.view" # This creates or updates if name exists
    params = {
        'u': username, 'p': password, 'v': '1.16.1', 'c': 'TuneForge', 
        'f': 'json', 'name': playlist_name
    }
    if track_ids: params['songId'] = track_ids
        
    debug_log(f"Navidrome: Creating/updating playlist '{playlist_name}' with {len(track_ids) if track_ids else 0} tracks.", "INFO", True)
    try:
        response = requests.get(url, params=params, timeout=30)
        # debug_log(f"Navidrome create playlist full URL: {response.url}", "DEBUG")
        response.raise_for_status()
        data = response.json()
        # debug_log(f"Navidrome create playlist response: {json.dumps(data)[:200]}...", "DEBUG")
        
        if data.get('subsonic-response', {}).get('status') == 'ok':
            playlist_data = data['subsonic-response'].get('playlist', {})
            playlist_id = playlist_data.get('id')
            actual_song_count = playlist_data.get('songCount', 'N/A')
            debug_log(f"Navidrome: Successfully created/updated playlist '{playlist_name}' (ID: {playlist_id}). Reported tracks: {actual_song_count}.", "INFO", True)
            return playlist_id
        else:
            error_message = data.get('subsonic-response', {}).get('error', {}).get('message', 'Unknown error')
            debug_log(f"Navidrome: Error creating playlist '{playlist_name}': {error_message}", "ERROR", True)
            return None
    except requests.exceptions.RequestException as e:
        debug_log(f"Navidrome: Request error creating playlist '{playlist_name}': {e}", "ERROR", True)
        return None
    except json.JSONDecodeError as e:
        debug_log(f"Navidrome: JSON decode error for playlist '{playlist_name}': {e}. Response: {response.text[:200] if 'response' in locals() else 'N/A'}", "ERROR", True)
        return None

def search_tracks_in_navidrome(navidrome_url, username, password, ollama_suggested_tracks, final_unique_matched_tracks_map):
    newly_matched_for_batch = []
    if not all([navidrome_url, username, password]):
        debug_log("Navidrome credentials/URL missing, skipping Navidrome search batch.", "WARN")
        return newly_matched_for_batch

    for suggested_track in ollama_suggested_tracks:
        title, artist = suggested_track.get("title"), suggested_track.get("artist")
        if not title or not artist: continue

        track_key = (title.lower(), artist.lower())
        if track_key in final_unique_matched_tracks_map: continue # Already found

        query = f"{artist} {title}" # Navidrome search query
        found_navidrome_tracks = search_track_in_navidrome(query, navidrome_url, username, password)

        best_match = None
        if found_navidrome_tracks:
            for nt in found_navidrome_tracks: # Find best match
                if nt['title'].lower() == title.lower() and nt['artist'].lower() == artist.lower():
                    best_match = nt; break
            if not best_match: best_match = found_navidrome_tracks[0] # Fallback to first
        
        if best_match:
            match_details = {
                'id': best_match['id'], 'title': best_match['title'], 'artist': best_match['artist'],
                'album': best_match['album'], 'source': 'navidrome', 
                'original_suggestion': {'title': title, 'artist': artist, 'album': suggested_track.get("album")}
            }
            final_unique_matched_tracks_map[track_key] = match_details
            newly_matched_for_batch.append(match_details)
            debug_log(f"Navidrome: Matched '{best_match['title']}' by '{best_match['artist']}' for suggestion '{title}' by '{artist}'.", "INFO")
        # else:
            # debug_log(f"Navidrome: No match for '{title}' by '{artist}'.", "DEBUG")
            
    return newly_matched_for_batch

# --- Plex Functions ---
def search_track_in_plex(plex_url, plex_token, title, artist, album, library_section_id):
    if not all([plex_url, plex_token, library_section_id]):
        debug_log("Plex URL, Token, or Library Section ID not configured. Skipping Plex search.", "WARN")
        return None

    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
    search_path = f"/library/sections/{library_section_id}/all"
    params = {'type': '10', 'title': title, 'grandparentTitle': artist, 'X-Plex-Token': plex_token}
    if album and album.lower() != "unknown album": params['parentTitle'] = album

    full_url = f"{plex_url.rstrip('/')}{search_path}"
    debug_log(f"Plex: Searching URL='{full_url}', Params='{ {k:v for k,v in params.items() if k != 'X-Plex-Token'} }'", "DEBUG")

    try:
        response = requests.get(full_url, headers=headers, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()

        if data.get('MediaContainer') and data['MediaContainer'].get('Metadata'):
            debug_log(f"Plex: Found {len(data['MediaContainer']['Metadata'])} potential matches for '{title}' by '{artist}'", "DEBUG")
            
            for track_info in data['MediaContainer']['Metadata']: # Iterate through results
                track_id = track_info.get('ratingKey')
                found_title = track_info.get('title')
                found_artist = track_info.get('grandparentTitle') # Artist
                found_album = track_info.get('parentTitle')    # Album
                track_section_id = track_info.get('librarySectionID')
                
                # Debug log each potential match's details
                debug_log(f"Plex: Checking match: Title='{found_title}', Artist='{found_artist}', Album='{found_album}', Track Section ID={track_section_id}, Expected Section ID={library_section_id}", "DEBUG")
                
                if track_id and found_title and found_artist:
                    # Check for section ID mismatch
                    if track_section_id and str(track_section_id) != str(library_section_id):
                        debug_log(f"Plex: Mismatched section ID for track '{found_title}' by '{found_artist}'. Track section: {track_section_id}, Expected: {library_section_id}", "DEBUG")
                        continue  # Skip this track due to section mismatch
                    
                    # Check for title/artist match
                    if found_title.lower() == title.lower() and found_artist.lower() == artist.lower():
                        debug_log(f"Plex: Found exact match: '{found_title}' by '{found_artist}' (ID: {track_id})", "DEBUG")
                        return {'id': track_id, 'title': found_title, 'artist': found_artist, 'album': found_album, 'source': 'plex'}
            
            debug_log(f"Plex: No exact match found for '{title}' by '{artist}' in results.", "DEBUG")
        else:
            debug_log(f"Plex: Track '{title}' by '{artist}' not found or no metadata in section {library_section_id}.", "DEBUG")
        return None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code != 404: # Don't log 404 as error here
             debug_log(f"Plex: HTTP error searching: {e}. Response: {e.response.text[:200]}", "WARN")
    except requests.exceptions.RequestException as e:
        debug_log(f"Plex: Request error searching: {e}", "WARN")
    except json.JSONDecodeError as e:
        debug_log(f"Plex: JSON decode error searching. Response: {response.text[:200] if 'response' in locals() else 'N/A'}", "WARN")
    return None

def create_playlist_in_plex(playlist_name, track_ids, plex_server_url, plex_token, plex_machine_id):
    if not all([plex_server_url, plex_token, plex_machine_id]):
        debug_log("Plex server URL, token, or machine ID missing. Cannot create playlist.", "ERROR", True)
        return None, 0
    if not track_ids:
        debug_log("No track IDs provided for Plex playlist creation.", "WARN", True)
        return None, 0

    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
    first_track_id = track_ids[0]
    
    create_playlist_url = f"{plex_server_url.rstrip('/')}/playlists"
    # URI for the item to create the playlist from (first track)
    # Format: server://{machine_id}/com.plexapp.plugins.library/library/metadata/{item_rating_key}
    source_item_uri = f"server://{plex_machine_id}/com.plexapp.plugins.library/library/metadata/{first_track_id}"
    
    create_params = {
        'X-Plex-Token': plex_token, 'title': playlist_name, 'smart': '0', 'type': 'audio',
        'uri': source_item_uri
    }
    
    playlist_rating_key = None
    created_tracks_count = 0

    debug_log(f"Plex: Attempting to create playlist '{playlist_name}' with first track ID: {first_track_id} (URI: {source_item_uri})", "INFO", True)
    # debug_log(f"Plex Create URL: {create_playlist_url}, Params: { {k:v for k,v in create_params.items() if k != 'X-Plex-Token'} }", "DEBUG")

    try:
        response = requests.post(create_playlist_url, headers=headers, params=create_params, timeout=30)
        # debug_log(f"Plex Create POST Status: {response.status_code}, Headers: {response.headers}", "DEBUG", True)
        # response_text_snippet = response.text[:1000] if response.text else "Empty"
        # debug_log(f"Plex Create POST Response Text (first 1000 chars): {response_text_snippet}", "DEBUG", True)
        response.raise_for_status() 
        
        created_playlist_data = response.json()
        if created_playlist_data.get('MediaContainer', {}).get('Metadata'):
            playlist_metadata = created_playlist_data['MediaContainer']['Metadata'][0]
            playlist_rating_key = playlist_metadata.get('ratingKey')
            created_tracks_count = int(playlist_metadata.get('leafCount', 0))
            debug_log(f"Plex: Playlist '{playlist_metadata.get('title')}' created. ID: {playlist_rating_key}, Initial items: {created_tracks_count}", "INFO", True)
        else:
            debug_log(f"Plex: Playlist created but response format unexpected: {json.dumps(created_playlist_data)[:500]}", "WARN", True)
            # Try to find ratingKey if possible, otherwise this will fail.
            # This path implies success (2xx) but unexpected JSON.

        if not playlist_rating_key:
            debug_log(f"Plex: Could not determine playlist ID from POST response.", "ERROR", True)
            return None, 0
        
        # At this point, the playlist is created with the first track.
        # created_tracks_count was set from the POST response (ideally 1).

        additional_track_ids = track_ids[1:]
        if additional_track_ids:
            add_items_url = f"{plex_server_url.rstrip('/')}/playlists/{playlist_rating_key}/items"
            items_uri_list = [f"server://{plex_machine_id}/com.plexapp.plugins.library/library/metadata/{tid}" for tid in additional_track_ids]
            items_uri_param = ",".join(items_uri_list)
            put_params = {'X-Plex-Token': plex_token, 'uri': items_uri_param}
            
            debug_log(f"Plex: Adding {len(additional_track_ids)} additional tracks to playlist ID {playlist_rating_key}.", "INFO", True)

            put_response = requests.put(add_items_url, headers=headers, params=put_params, timeout=60)
            debug_log(f"Plex Add Items PUT Status: {put_response.status_code}", "DEBUG", True)
            put_response.raise_for_status()

            updated_playlist_data = put_response.json()
            # Check the response from the PUT request to confirm tracks were added.
            if updated_playlist_data.get('MediaContainer', {}).get('Metadata'):
                playlist_meta_put = updated_playlist_data['MediaContainer']['Metadata'][0]
                # leafCountAdded is the most reliable field if present
                leaf_count_added_str = playlist_meta_put.get('leafCountAdded')
                if leaf_count_added_str is not None:
                    leaf_count_added = int(leaf_count_added_str)
                    # The initial track is already counted in created_tracks_count from POST
                    # So, we add the newly added tracks from PUT.
                    # However, the total count is in 'leafCount' or 'size'.
                    # Let's use the final leafCount from the PUT response directly.
                    final_leaf_count_str = playlist_meta_put.get('leafCount', playlist_meta_put.get('size'))
                    if final_leaf_count_str is not None:
                        final_leaf_count = int(final_leaf_count_str)
                        debug_log(f"Plex: Playlist updated. leafCountAdded: {leaf_count_added}, Final leafCount: {final_leaf_count}", "INFO", True)
                        created_tracks_count = final_leaf_count # This is the total number of tracks in the playlist
                    else:
                        # If leafCount is not available, but leafCountAdded is, it implies an issue or partial success.
                        # We can be conservative or try to infer. For now, let's assume the reported added count is on top of the first one.
                        debug_log(f"Plex: PUT successful, leafCountAdded: {leaf_count_added}, but final leafCount missing. Assuming initial + added.", "WARN")
                        created_tracks_count = created_tracks_count + leaf_count_added # created_tracks_count is 1 from POST
                else:
                    # If 'leafCountAdded' is not present, try to use 'leafCount' or 'size' from PUT response
                    final_leaf_count_str = playlist_meta_put.get('leafCount', playlist_meta_put.get('size'))
                    if final_leaf_count_str is not None:
                        final_leaf_count = int(final_leaf_count_str)
                        debug_log(f"Plex: Playlist updated. Final leafCount (from PUT JSON): {final_leaf_count}. leafCountAdded was missing.", "INFO", True)
                        created_tracks_count = final_leaf_count
                    else:
                        debug_log(f"Plex: Add items PUT successful (status {put_response.status_code}), but response format for counts unexpected. Current count: {created_tracks_count}", "WARN")
                        # created_tracks_count remains as it was from the POST (likely 1), as we can't confirm more were added from PUT response.
            else:
                debug_log(f"Plex: Add items PUT successful (status {put_response.status_code}), but MediaContainer or Metadata missing in response. Count remains {created_tracks_count}.", "WARN")
                # created_tracks_count remains as it was from the POST (likely 1)
        
        if created_tracks_count != len(track_ids):
             debug_log(f"Plex: Playlist item count mismatch. Expected {len(track_ids)}, got {created_tracks_count}. Check Plex server.", "WARN")
        else:
             debug_log(f"Plex: Playlist successfully created/updated with {created_tracks_count} tracks.", "INFO", True)

        return playlist_rating_key, created_tracks_count

    except requests.exceptions.HTTPError as e:
        error_details = e.response.text[:500] if e.response else "No response body"
        debug_log(f"Plex: HTTP error during playlist operation for '{playlist_name}': {e}. Status: {e.response.status_code if e.response else 'N/A'}. Details: {error_details}", "ERROR", True)
        return None, 0
    except requests.exceptions.RequestException as e:
        debug_log(f"Plex: Request error during playlist operation for '{playlist_name}': {e}", "ERROR", True)
        return None, 0
    except json.JSONDecodeError as e:
        resp_text = "N/A"
        if 'response' in locals() and response: resp_text = response.text[:200]
        elif 'put_response' in locals() and put_response: resp_text = put_response.text[:200]
        debug_log(f"Plex: JSON decode error during playlist op for '{playlist_name}': {e}. Response: {resp_text}", "ERROR", True)
        return None, 0
    except Exception as e:
        debug_log(f"Plex: Unexpected error during playlist op for '{playlist_name}': {e}", "ERROR", True)
        return None, 0

def search_tracks_in_plex(plex_url, plex_token, ollama_suggested_tracks, final_unique_matched_tracks_map, library_section_id):
    newly_matched_for_batch = []
    if not all([plex_url, plex_token, library_section_id]):
        debug_log("Plex credentials/URL/SectionID missing, skipping Plex search batch.", "WARN")
        return newly_matched_for_batch

    for suggested_track in ollama_suggested_tracks:
        title, artist, album = suggested_track.get("title"), suggested_track.get("artist"), suggested_track.get("album", "Unknown Album")
        if not title or not artist: continue

        track_key = (title.lower(), artist.lower())
        if track_key in final_unique_matched_tracks_map: continue

        found_plex_track = search_track_in_plex(plex_url, plex_token, title, artist, album, library_section_id)
        if found_plex_track:
            match_details = {
                'id': found_plex_track['id'], 'title': found_plex_track['title'], 'artist': found_plex_track['artist'],
                'album': found_plex_track['album'], 'source': 'plex',
                'original_suggestion': {'title': title, 'artist': artist, 'album': album}
            }
            final_unique_matched_tracks_map[track_key] = match_details
            newly_matched_for_batch.append(match_details)
            debug_log(f"Plex: Matched '{found_plex_track['title']}' by '{found_plex_track['artist']}' for suggestion '{title}' by '{artist}'.", "INFO")
        # else:
            # debug_log(f"Plex: No match for '{title}' by '{artist}' (Album: '{album}') in section {library_section_id}.", "DEBUG")
            
    return newly_matched_for_batch

def test_plex_connection(plex_url, plex_token):
    result = {'success': False, 'error': None, 'message': 'Test not fully executed.', 'details': {}, 'server_info': None}
    if not all([plex_url, plex_token]):
        result['error'] = "Plex Server URL or Token missing."
        result['message'] = "Plex Server URL and Token are required."
        return result

    base_url = plex_url.rstrip('/')
    identity_url = f"{base_url}/identity"
    result['details']['attempted_url'] = identity_url

    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
    response = None  # Initialize response variable
    
    try:
        response = requests.get(identity_url, headers=headers, timeout=10)
        result['details']['status_code'] = response.status_code
        
        try:
            result['details']['response_snippet'] = response.text[:500]
        except Exception:
            result['details']['response_snippet'] = "Could not retrieve response text."

        if response.status_code == 200:
            data = response.json()
            mc = data.get('MediaContainer', {})
            server_info_data = {
                'friendlyName': mc.get('friendlyName'),
                'machineIdentifier': mc.get('machineIdentifier'),
                'version': mc.get('version'),
                'platform': mc.get('platform'),
                'platformVersion': mc.get('platformVersion'),
            }
            result['server_info'] = server_info_data
            
            if mc.get('machineIdentifier'):
                 result['success'] = True
                 result['message'] = f"Successfully connected to Plex server: {server_info_data.get('friendlyName', 'Unknown Name')} (Version: {server_info_data.get('version', 'Unknown')})"
            else:
                result['error'] = "Connected, but couldn't retrieve essential server identity (e.g., Machine ID)."
                result['message'] = "Connection attempt returned 200 OK, but the response format was unexpected for server identity."

        elif response.status_code == 401:
            result['error'] = "Plex connection failed: Unauthorized (401). Check your Plex Token."
            result['message'] = "Authentication failed. Please verify your Plex token."
        else:
            response.raise_for_status()

    except requests.exceptions.HTTPError as e:
        result['error'] = f"Plex connection failed: HTTP Error {e.response.status_code if e.response else 'Unknown'} when accessing {identity_url}."
        result['message'] = f"Server returned an HTTP error: {e.response.status_code if e.response else 'Unknown'}."
        if e.response and e.response.text:
             result['details']['response_snippet'] = e.response.text[:500]
    except requests.exceptions.ConnectionError:
        result['error'] = f"Plex connection failed: Could not connect to server at {plex_url} (tried {identity_url})."
        result['message'] = "Unable to establish a connection with the Plex server. Check the URL and network."
    except requests.exceptions.Timeout:
        result['error'] = f"Plex connection failed: Connection timed out when accessing {identity_url}."
        result['message'] = "The connection to the Plex server timed out."
    except json.JSONDecodeError:
        result['error'] = f"Plex connection failed: Could not parse JSON response from {identity_url}."
        result['message'] = "Received an invalid JSON response from the server."
        if response and response.text: # response_snippet might already be set
             result['details']['response_snippet'] = response.text[:500]
    except requests.exceptions.RequestException as e:
        result['error'] = f"Plex connection failed: A request error occurred - {str(e)}."
        result['message'] = f"An unexpected error occurred during the request: {str(e)}."
    except Exception as e:
        result['error'] = f"An unexpected error occurred: {str(e)}"
        result['message'] = "An unexpected error occurred during the Plex connection test."
        debug_log(f"Unexpected error in test_plex_connection: {e}", "ERROR", True)

    return result

# --- Flask Routes ---
@main_bp.route('/')
def index():
    return render_template('index.html')

@main_bp.route('/history')
def history():
    playlist_history = load_playlist_history()
    return render_template('history.html', history=playlist_history)

@main_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        # This is form data, not JSON
        current_config = load_config() # Load existing to preserve sections/keys not in form
        
        # OLLAMA section
        if not current_config.has_section('OLLAMA'): current_config.add_section('OLLAMA')
        current_config.set('OLLAMA', 'URL', request.form.get('ollama_url', get_config_value('OLLAMA', 'URL', '')))
        current_config.set('OLLAMA', 'Model', request.form.get('ollama_model', get_config_value('OLLAMA', 'Model', '')))
        current_config.set('OLLAMA', 'ContextWindow', request.form.get('context_window', get_config_value('OLLAMA', 'ContextWindow', '2048')))
        current_config.set('OLLAMA', 'MaxAttempts', request.form.get('max_attempts', get_config_value('OLLAMA', 'MaxAttempts', '3')))
        current_config.set('OLLAMA', 'Temperature', request.form.get('ollama_temperature', get_config_value('OLLAMA', 'Temperature', '0.7')))
        current_config.set('OLLAMA', 'TopP', request.form.get('ollama_top_p', get_config_value('OLLAMA', 'TopP', '0.9')))
        current_config.set('OLLAMA', 'DebugOllamaResponse', request.form.get('debug_ollama_response', get_config_value('OLLAMA', 'DebugOllamaResponse', 'no')))


        # APP section
        if not current_config.has_section('APP'): current_config.add_section('APP')
        current_config.set('APP', 'Likes', request.form.get('likes', get_config_value('APP', 'Likes', '')))
        current_config.set('APP', 'Dislikes', request.form.get('dislikes', get_config_value('APP', 'Dislikes', '')))
        current_config.set('APP', 'FavoriteArtists', request.form.get('favorite_artists', get_config_value('APP', 'FavoriteArtists', '')))
        current_config.set('APP', 'EnableNavidrome', request.form.get('enable_navidrome', get_config_value('APP', 'EnableNavidrome', 'no')))
        current_config.set('APP', 'EnablePlex', request.form.get('enable_plex', get_config_value('APP', 'EnablePlex', 'no')))
        current_config.set('APP', 'Debug', request.form.get('app_debug_mode', get_config_value('APP', 'Debug', 'yes'))) # Ensure this matches the form field name


        # NAVIDROME section
        if not current_config.has_section('NAVIDROME'): current_config.add_section('NAVIDROME')
        current_config.set('NAVIDROME', 'URL', request.form.get('navidrome_url', get_config_value('NAVIDROME', 'URL', '')))
        current_config.set('NAVIDROME', 'Username', request.form.get('navidrome_username', get_config_value('NAVIDROME', 'Username', '')))
        current_config.set('NAVIDROME', 'Password', request.form.get('navidrome_password', get_config_value('NAVIDROME', 'Password', '')))
        
        # PLEX section
        if not current_config.has_section('PLEX'): current_config.add_section('PLEX')
        current_config.set('PLEX', 'ServerURL', request.form.get('plex_server_url', get_config_value('PLEX', 'ServerURL', '')))
        current_config.set('PLEX', 'Token', request.form.get('plex_token', get_config_value('PLEX', 'Token', '')))
        current_config.set('PLEX', 'MachineID', request.form.get('plex_machine_id', get_config_value('PLEX', 'MachineID', '')))
        current_config.set('PLEX', 'MusicSectionID', request.form.get('plex_music_section_id', get_config_value('PLEX', 'MusicSectionID', '')))
        current_config.set('PLEX', 'PlaylistType', request.form.get('plex_playlist_type', get_config_value('PLEX', 'PlaylistType', 'audio')))


        with open(CONFIG_FILE, 'w') as configfile:
            current_config.write(configfile)
        # Instead of redirect, return JSON for AJAX handling
        return jsonify({'status': 'success', 'message': 'Settings saved successfully!'})

    # GET request
    context = {
        'ollama_url': get_config_value('OLLAMA', 'URL', 'http://localhost:11434'),
        'ollama_model': get_config_value('OLLAMA', 'Model', 'llama3'),
        'context_window': get_config_value('OLLAMA', 'ContextWindow', '2048'),
        'max_attempts': get_config_value('OLLAMA', 'MaxAttempts', '3'),
        'ollama_temperature': get_config_value('OLLAMA', 'Temperature', '0.7'),
        'ollama_top_p': get_config_value('OLLAMA', 'TopP', '0.9'),
        'debug_ollama_response': get_config_value('OLLAMA', 'DebugOllamaResponse', 'no'),

        'likes': get_config_value('APP', 'Likes', ''),
        'dislikes': get_config_value('APP', 'Dislikes', ''),
        'favorite_artists': get_config_value('APP', 'FavoriteArtists', ''),
        'enable_navidrome': get_config_value('APP', 'EnableNavidrome', 'no'),
        'enable_plex': get_config_value('APP', 'EnablePlex', 'no'),
        'app_debug_mode': get_config_value('APP', 'Debug', 'yes'), # Ensure this matches the form field name and context variable

        'navidrome_url': get_config_value('NAVIDROME', 'URL', ''),
        'navidrome_username': get_config_value('NAVIDROME', 'Username', ''),
        'navidrome_password': get_config_value('NAVIDROME', 'Password', ''),
        
        'plex_server_url': get_config_value('PLEX', 'ServerURL', ''),
        'plex_token': get_config_value('PLEX', 'Token', ''),
        'plex_machine_id': get_config_value('PLEX', 'MachineID', ''),
        'plex_playlist_type': get_config_value('PLEX', 'PlaylistType', 'audio'), 
        'plex_music_section_id': get_config_value('PLEX', 'MusicSectionID', '')
    }
    return render_template('settings.html', **context)

@main_bp.route('/test-navidrome') # Renders the test page
def test_navidrome_page():
    return render_template('test_navidrome.html')

@main_bp.route('/api/test-navidrome-connection', methods=['POST']) # API endpoint for testing
def api_test_navidrome_connection():
    data = request.get_json() # Use get_json() for better error handling
    if not data:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400
    navidrome_url = data.get('navidrome_url')
    username = data.get('username')
    password = data.get('password')
    result = test_navidrome_connection(navidrome_url, username, password)
    return jsonify(result)

@main_bp.route('/test-plex') # Renders the Plex test page
def test_plex_page():
    context = {
        'plex_server_url': get_config_value('PLEX', 'ServerURL', ''),
        'plex_token': get_config_value('PLEX', 'Token', ''),
        'plex_music_section_id': get_config_value('PLEX', 'MusicSectionID', '')
    }
    return render_template('test_plex.html', **context)

@main_bp.route('/api/test-plex-connection', methods=['POST']) # API endpoint for testing Plex connection
def api_test_plex_connection():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid or missing JSON payload", "success": False}), 400
    
    plex_server_url = data.get('plex_server_url')
    plex_token = data.get('plex_token')

    if not plex_server_url or not plex_token:
        return jsonify({"error": "Plex Server URL and Token are required in the payload.", "success": False}), 400

    result = test_plex_connection(plex_server_url, plex_token)
    return jsonify(result)

@main_bp.route('/api/generate-playlist', methods=['POST'])
def api_generate_playlist():
    data = request.get_json() # Use get_json() for better error handling
    if not data:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400
        
    prompt = data.get('prompt')
    playlist_name = data.get('playlist_name', 'New TuneForge Playlist')
    num_songs = int(data.get('num_songs', 10))
    services_to_use_req = data.get('services', [])
    services_to_use = [s.lower() for s in services_to_use_req] if services_to_use_req else ['navidrome', 'plex']
    
    max_ollama_attempts = int(get_config_value('OLLAMA', 'MaxAttempts', '3'))

    if not prompt: return jsonify({"error": "Prompt is required"}), 400

    ollama_url = get_config_value('OLLAMA', 'URL')
    ollama_model = get_config_value('OLLAMA', 'Model')
    if not ollama_url or not ollama_model:
        return jsonify({"error": "Ollama URL or Model not configured in settings."}), 400
    
    enable_navidrome = 'navidrome' in services_to_use and get_config_value('APP', 'EnableNavidrome', 'no').lower() in ('yes', 'true', '1')
    enable_plex = 'plex' in services_to_use and get_config_value('APP', 'EnablePlex', 'no').lower() in ('yes', 'true', '1')

    navidrome_url, navidrome_user, navidrome_pass = (None,)*3
    if enable_navidrome:
        navidrome_url = get_config_value('NAVIDROME', 'URL')
        navidrome_user = get_config_value('NAVIDROME', 'Username')
        navidrome_pass = get_config_value('NAVIDROME', 'Password')
        if not all([navidrome_url, navidrome_user, navidrome_pass]):
            debug_log("Navidrome enabled but details missing. Disabling for this run.", "WARN", True); enable_navidrome = False

    plex_server_url, plex_token, plex_section_id, plex_machine_id = (None,)*4
    if enable_plex:
        plex_server_url = get_config_value('PLEX', 'ServerURL')
        plex_token = get_config_value('PLEX', 'Token')
        plex_section_id = get_config_value('PLEX', 'MusicSectionID')
        plex_machine_id = get_config_value('PLEX', 'MachineID')
        if not all([plex_server_url, plex_token, plex_section_id, plex_machine_id]):
            debug_log("Plex enabled but critical details missing. Disabling for this run.", "WARN", True); enable_plex = False
    
    if not enable_navidrome and not enable_plex:
        return jsonify({"error": "No services (Navidrome/Plex) are enabled or properly configured."}), 400

    final_unique_matched_tracks_map = {} # Stores {(title.lower(), artist.lower()): track_details_dict}
    all_ollama_suggestions_raw = [] # Stores all raw track dicts from Ollama for context
    
    ollama_api_calls_made = 0
    
    while len(final_unique_matched_tracks_map) < num_songs and ollama_api_calls_made < max_ollama_attempts:
        tracks_still_needed = num_songs - len(final_unique_matched_tracks_map)
        debug_log(f"Playlist Gen: Attempt {ollama_api_calls_made + 1}/{max_ollama_attempts}. Tracks found: {len(final_unique_matched_tracks_map)}/{num_songs}.", "INFO", True)

        songs_to_request_this_ollama_call = tracks_still_needed * 2 # Request more to account for filtering/matching
        if tracks_still_needed <= 3: songs_to_request_this_ollama_call = tracks_still_needed + 7
        songs_to_request_this_ollama_call = max(5, min(songs_to_request_this_ollama_call, 20)) # Bounds: 5-20

        debug_log(f"Ollama: Requesting {songs_to_request_this_ollama_call} new tracks.", "INFO")
        current_ollama_batch = generate_tracks_with_ollama(
            ollama_url, ollama_model, prompt, songs_to_request_this_ollama_call, 
            ollama_api_calls_made, all_ollama_suggestions_raw
        )
        ollama_api_calls_made += 1

        if not current_ollama_batch:
            debug_log(f"Ollama: Attempt {ollama_api_calls_made} yielded no tracks. {'Retrying...' if ollama_api_calls_made < max_ollama_attempts else 'Max attempts reached.'}", "WARN", True)
            if ollama_api_calls_made < max_ollama_attempts: time.sleep(1); continue
            else: break # Max Ollama attempts reached

        all_ollama_suggestions_raw.extend(current_ollama_batch)
        
        # Pre-filter Ollama suggestions (live, instrumental, already found etc.)
        undesirable_patterns = [ # Regex patterns
            r"\(live\b", r"\[live\b", r"- live\b", r"\blive at\b", r"\blive from\b",
            r"\(instrumental\b", r"\[instrumental\b", r"- instrumental\b",
            r"\(karaoke\b", r"\[karaoke\b", r"- karaoke\b", r"karaoke version\b",
            r"\(cover\b", r"\[cover\b", r"- cover\b", r" tribute\b",
            r"\(remix\b", r"\[remix\b", r"- remix\b",
            r"\(acoustic\b", r"\[acoustic\b", r"- acoustic\b",
            r"\(edit\b", r"\[edit\b", r"- radio edit\b", r"single version\b",
            r"\(demo\b", r"\[demo\b", r"- demo\b", r"\(session\b", r"\[session\b",
        ]
        undesirable_artist_keywords = ["karaoke", "tribute band", "the karaoke crew", "various artists", "soundtrack"]
        
        eligible_tracks_for_search = []
        for track in current_ollama_batch:
            title_l, artist_l, album_l = track.get("title","").lower(), track.get("artist","").lower(), track.get("album","").lower()
            if not title_l or not artist_l: continue
            if (title_l, artist_l) in final_unique_matched_tracks_map: continue # Already found

            is_undesirable = any(re.search(p, title_l) or re.search(p, album_l) for p in undesirable_patterns) or \
                             any(k in artist_l for k in undesirable_artist_keywords)
            if is_undesirable:
                # debug_log(f"Filtering out undesirable: '{track.get('title')}' by '{track.get('artist')}'", "DEBUG")
                continue
            eligible_tracks_for_search.append(track)
        
        debug_log(f"Ollama: {len(eligible_tracks_for_search)} tracks eligible for searching after filtering batch of {len(current_ollama_batch)}.", "INFO")
        if not eligible_tracks_for_search: continue

        if enable_navidrome:
            search_tracks_in_navidrome(navidrome_url, navidrome_user, navidrome_pass, eligible_tracks_for_search, final_unique_matched_tracks_map)
            if len(final_unique_matched_tracks_map) >= num_songs: break
        
        if enable_plex:
            search_tracks_in_plex(plex_server_url, plex_token, eligible_tracks_for_search, final_unique_matched_tracks_map, plex_section_id)
            if len(final_unique_matched_tracks_map) >= num_songs: break
        
        if ollama_api_calls_made < max_ollama_attempts and len(final_unique_matched_tracks_map) < num_songs : time.sleep(0.5)

    # --- End of main generation loop ---
    final_tracklist_details = list(final_unique_matched_tracks_map.values())

    if not final_tracklist_details:
        msg = f"Could not find any tracks for prompt '{prompt}' after {ollama_api_calls_made} Ollama attempts."
        debug_log(msg, "ERROR", True)
        return jsonify({"message": msg, "playlist_name": playlist_name, "tracks_found": 0}), 500

    debug_log(f"Playlist Gen: Total {len(final_tracklist_details)} unique tracks found for playlist '{playlist_name}'.", "INFO", True)

    created_playlists_summary = {}
    navidrome_ids = [t['id'] for t in final_tracklist_details if t['source'] == 'navidrome']
    plex_ids = [t['id'] for t in final_tracklist_details if t['source'] == 'plex']

    if enable_navidrome:
        if navidrome_ids:
            nid = create_playlist_in_navidrome(navidrome_url, navidrome_user, navidrome_pass, playlist_name, navidrome_ids)
            created_playlists_summary['navidrome'] = {'id': nid, 'track_count': len(navidrome_ids) if nid else 0, 'status': 'success' if nid else 'failed'}
        else: created_playlists_summary['navidrome'] = {'id': None, 'track_count': 0, 'status': 'no_tracks'}

    if enable_plex:
        if plex_ids:
            pid, plex_count = create_playlist_in_plex(playlist_name, plex_ids, plex_server_url, plex_token, plex_machine_id)
            created_playlists_summary['plex'] = {'id': pid, 'track_count': plex_count if pid else 0, 'status': 'success' if pid else 'failed'}
        else: created_playlists_summary['plex'] = {'id': None, 'track_count': 0, 'status': 'no_tracks'}

    history = load_playlist_history()
    history.append({
        "name": playlist_name, "prompt": prompt, "num_songs_requested": num_songs,
        "num_songs_added_total": len(final_tracklist_details),
        "services_targeted": services_to_use, "creation_results": created_playlists_summary,
        "tracks_details": final_tracklist_details, "timestamp": datetime.datetime.now().isoformat()
    })
    save_playlist_history(history)

    return jsonify({
        "message": f"Playlist '{playlist_name}' generation complete. Found {len(final_tracklist_details)}/{num_songs} tracks.",
        "playlist_name": playlist_name, "tracks_added_count": len(final_tracklist_details),
        "target_song_count": num_songs, "created_in_services": created_playlists_summary,
        "ollama_api_calls": ollama_api_calls_made
    }), 200

@main_bp.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        data = request.get_json() # Use get_json() for better error handling
        if not data:
            return jsonify({"error": "Invalid or missing JSON payload"}), 400
        try:
            save_config(data) # data is expected to be a dict of dicts like {section: {key: value}}
            return jsonify({"message": "Configuration saved successfully"}), 200
        except Exception as e:
            debug_log(f"Error saving configuration via API: {e}", "ERROR", True)
            return jsonify({"error": f"Failed to save configuration: {str(e)}"}), 500
    else: # GET
        config_parser = load_config()
        config_data_to_send = {section: dict(config_parser.items(section)) for section in config_parser.sections()}
        return jsonify(config_data_to_send)

@main_bp.route('/api/history', methods=['GET'])
def api_history():
    history = load_playlist_history()
    return jsonify(history)

@main_bp.route('/api/plex_fetch_libraries', methods=['GET'])
def plex_fetch_libraries_route():
    plex_url = get_config_value('PLEX', 'ServerURL')
    plex_token = get_config_value('PLEX', 'Token')
    if not plex_url or not plex_token:
        return jsonify({"error": "Plex ServerURL or Token not configured."}), 400

    try:
        headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
        response = requests.get(f"{plex_url.rstrip('/')}/library/sections", headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        libraries = []
        if data.get('MediaContainer', {}).get('Directory'):
            for lib in data['MediaContainer']['Directory']:
                if lib.get('type') == 'artist': # Music libraries
                    libraries.append({'id': lib.get('key'), 'name': lib.get('title'), 'type': lib.get('type')})
        return jsonify(libraries)
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout connecting to Plex server."}), 504
    except requests.exceptions.RequestException as e:
        err_msg = str(e)
        status = e.response.status_code if e.response is not None else 500
        try: 
            if e.response is not None: # Ensure response object exists
                err_msg = e.response.json().get('errors',[{}])[0].get('message', str(e))
        except: pass # Keep original if parsing fails
        debug_log(f"Error fetching Plex libraries: {err_msg}", "ERROR")
        return jsonify({"error": f"Failed to fetch Plex libraries: {err_msg}"}), status
    except Exception as e: # Catch-all for other errors like JSONDecodeError if not caught by RequestException
        debug_log(f"Unexpected error fetching Plex libraries: {e}", "ERROR")
        return jsonify({"error": "An unexpected error occurred."}), 500


@main_bp.route('/api/plex_fetch_machine_id', methods=['GET'])
def plex_fetch_machine_id_route():
    plex_url = get_config_value('PLEX', 'ServerURL')
    plex_token = get_config_value('PLEX', 'Token')
    if not plex_url or not plex_token:
        return jsonify({"error": "Plex ServerURL or Token not configured."}), 400

    try:
        headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
        # Try /identity first, then fallback to /
        for endpoint in ['/identity', '/']:
            response = requests.get(f"{plex_url.rstrip('/')}{endpoint}", headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            machine_id = data.get('MediaContainer', {}).get('machineIdentifier')
            if machine_id:
                return jsonify({"machine_identifier": machine_id})
        
        return jsonify({"error": "Could not determine Plex Machine Identifier from / or /identity."}), 404

    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout connecting to Plex server for machine ID."}), 504
    except requests.exceptions.RequestException as e:
        err_msg = str(e)
        status = e.response.status_code if e.response is not None else 500
        try: 
            if e.response is not None: # Ensure response object exists
                err_msg = e.response.json().get('errors',[{}])[0].get('message', str(e))
        except: pass
        debug_log(f"Error fetching Plex machine ID: {err_msg}", "ERROR")
        return jsonify({"error": f"Failed to fetch Plex machine ID: {err_msg}"}), status
    except Exception as e:
        debug_log(f"Unexpected error fetching Plex machine ID: {e}", "ERROR")
        return jsonify({"error": "An unexpected error occurred."}), 500
