#!/usr/bin/env python3
"""
Spotify-2-AppleMusic Sync Daemon
Sincronización automática e incremental entre Spotify y Apple Music sin requerir
Apple Developer Program.
"""

from __future__ import annotations
import os
import sys
import time
import json
import re
import base64
import argparse
import warnings
import urllib.parse
from datetime import datetime
from typing import Optional, List, Dict, Set

# Silenciar advertencia cosmética de urllib3 v2 en versiones antiguas de macOS/LibreSSL
warnings.filterwarnings("ignore", category=Warning, module="urllib3")

import requests
from bs4 import BeautifulSoup
import dateparser


# Intentar cargar python-dotenv si está disponible
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==============================================================================
# GESTIÓN DE CONFIGURACIÓN Y CREDENCIALES
# ==============================================================================

def read_file_if_exists(filepath: str) -> str:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

def load_config():
    config = {
        "spotify_client_id": os.getenv("SPOTIFY_CLIENT_ID") or read_file_if_exists("spotify_client_id.dat"),
        "spotify_client_secret": os.getenv("SPOTIFY_CLIENT_SECRET") or read_file_if_exists("spotify_client_secret.dat"),
        "spotify_refresh_token": os.getenv("SPOTIFY_REFRESH_TOKEN", ""),
        "apple_token": os.getenv("APPLE_MUSIC_TOKEN") or read_file_if_exists("token.dat"),
        "apple_media_user_token": os.getenv("APPLE_MUSIC_MEDIA_USER_TOKEN") or read_file_if_exists("media_user_token.dat"),
        "apple_cookies": os.getenv("APPLE_MUSIC_COOKIES") or read_file_if_exists("cookies.dat"),
        "apple_storefront": (os.getenv("APPLE_MUSIC_STOREFRONT") or read_file_if_exists("country_code.dat") or "US").upper(),
        "playlists_env": os.getenv("PLAYLISTS", ""),
        "sync_interval": int(os.getenv("SYNC_INTERVAL", "3600")),
        "discord_webhook": os.getenv("DISCORD_WEBHOOK_URL", "")
    }

    # Asegurar prefijo Bearer en token de Apple
    if config["apple_token"] and not config["apple_token"].startswith("Bearer "):
        config["apple_token"] = f"Bearer {config['apple_token']}"

    return config

def notify(webhook_url: str, title: str, description: str, is_error: bool = False):
    """Envía notificación a Discord Webhook si está configurado."""
    if not webhook_url:
        return
    try:
        color = 0xFF0033 if is_error else 0x1DB954  # Rojo o Verde Spotify
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.utcnow().isoformat()
            }]
        }
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Aviso] No se pudo enviar notificación webhook: {e}")

# ==============================================================================
# CLIENTE SPOTIFY
# ==============================================================================

class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str = ""):
        if not client_id or not client_secret:
            raise ValueError("Faltan SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET en la configuración (.env).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self.token_expiry = 0

    def get_token(self) -> str:
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token

        auth_str = f"{self.client_id}:{self.client_secret}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()

        # Si tenemos refresh_token de usuario, renovar sesión de usuario (permite leer playlists completas sin límite)
        if self.refresh_token:
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token
            }
        else:
            payload = {"grant_type": "client_credentials"}

        res = requests.post(
            "https://accounts.spotify.com/api/token",
            data=payload,
            headers={"Authorization": f"Basic {auth_b64}"},
            timeout=10
        )
        if res.status_code != 200:
            raise Exception(f"Error al autenticar en Spotify: {res.status_code} - {res.text}")

        data = res.json()
        self.access_token = data["access_token"]
        self.token_expiry = time.time() + data.get("expires_in", 3600) - 60
        return self.access_token

    @staticmethod
    def extract_playlist_id(source: str) -> str:
        """Extrae el ID limpio de Spotify a partir de una URL, URI o ID."""
        source = source.strip()
        if "open.spotify.com/playlist/" in source:
            part = source.split("open.spotify.com/playlist/")[1]
            return part.split("?")[0].split("/")[0]
        if source.startswith("spotify:playlist:"):
            return source.split("spotify:playlist:")[1]
        return source

    def get_playlist_metadata(self, playlist_id: str) -> dict:
        token = self.get_token()
        clean_id = self.extract_playlist_id(playlist_id)
        if clean_id.upper() in ("LIKES", "SAVED", "ME", "LIKED_SONGS"):
            res = requests.get(
                "https://api.spotify.com/v1/me/tracks?limit=1",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            total = res.json().get("total", 0) if res.status_code == 200 else 0
            return {"id": "LIKES", "name": "Canciones que te gustan (Likes)", "description": "Tus canciones favoritas de Spotify", "tracks": {"total": total}}

        res = requests.get(
            f"https://api.spotify.com/v1/playlists/{clean_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,name,description,tracks.total"},
            timeout=10
        )
        if res.status_code != 200:
            raise Exception(f"No se pudo obtener información de la playlist de Spotify ({clean_id}): {res.status_code} - {res.text}")
        return res.json()

    def get_all_tracks(self, playlist_id: str) -> list:
        """Obtiene todas las canciones de la playlist (o Likes) de Spotify con paginación y fallback automático."""
        clean_id = self.extract_playlist_id(playlist_id)
        tracks = []

        # Caso especial: Likes del usuario (endpoint /v1/me/tracks)
        if clean_id.upper() in ("LIKES", "SAVED", "ME", "LIKED_SONGS"):
            token = self.get_token()
            limit = 50  # Máximo permitido por Spotify en /me/tracks
            offset = 0
            while True:
                res = requests.get(
                    "https://api.spotify.com/v1/me/tracks",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": limit, "offset": offset},
                    timeout=15
                )
                if res.status_code != 200:
                    raise Exception(f"Error al leer tus likes de Spotify: {res.status_code} - {res.text}")
                data = res.json()
                items = data.get("items", [])
                if not items:
                    break
                for item in items:
                    track = item.get("track")
                    if not track or not track.get("id"):
                        continue
                    artists = track.get("artists", [])
                    artist_names = [a.get("name", "") for a in artists if a.get("name")]
                    primary_artist = artist_names[0] if artist_names else "Unknown Artist"
                    album = track.get("album", {})
                    album_artists = [a.get("name", "") for a in album.get("artists", [])]
                    album_artist = album_artists[0] if album_artists else primary_artist
                    tracks.append({
                        "spotify_id": track.get("id"),
                        "title": track.get("name", ""),
                        "artist": primary_artist,
                        "all_artists": ", ".join(artist_names),
                        "album": album.get("name", ""),
                        "album_artist": album_artist,
                        "release_date": album.get("release_date", ""),
                        "isrc": track.get("external_ids", {}).get("isrc", "")
                    })
                offset += len(items)
                if not data.get("next"):
                    break
            return tracks

        # Intento 1: API oficial de Spotify (probando /items primero, luego /tracks)
        try:
            token = self.get_token()
            limit = 50
            offset = 0

            # Determinar endpoint soportado (/items en API nueva, /tracks en versiones anteriores)
            endpoint = f"https://api.spotify.com/v1/playlists/{clean_id}/items"
            test_res = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                params={"limit": 1},
                timeout=10
            )
            if test_res.status_code == 404:
                endpoint = f"https://api.spotify.com/v1/playlists/{clean_id}/tracks"

            while True:
                res = requests.get(
                    endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": limit, "offset": offset},
                    timeout=15
                )
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("items", [])
                    if not items:
                        break
                    for item in items:
                        track = item.get("item") or item.get("track")
                        if not track or not track.get("id"):
                            continue
                        artists = track.get("artists", [])
                        artist_names = [a.get("name", "") for a in artists if a.get("name")]
                        primary_artist = artist_names[0] if artist_names else "Unknown Artist"
                        album = track.get("album", {})
                        album_artists = [a.get("name", "") for a in album.get("artists", [])]
                        album_artist = album_artists[0] if album_artists else primary_artist
                        tracks.append({
                            "spotify_id": track.get("id"),
                            "title": track.get("name", ""),
                            "artist": primary_artist,
                            "all_artists": ", ".join(artist_names),
                            "album": album.get("name", ""),
                            "album_artist": album_artist,
                            "release_date": album.get("release_date", ""),
                            "isrc": track.get("external_ids", {}).get("isrc", "")
                        })
                    offset += len(items)
                    if not data.get("next"):
                        break
                elif res.status_code == 403:
                    print("        [Info] API oficial restringió lectura directa (403). Usando extractor público...")
                    tracks = []
                    break
                else:
                    break
            if tracks:
                return tracks
        except Exception as e:
            print(f"        [Info] Fallback a extractor web por: {e}")

        # Intento 2: Extractor web directo de Spotify (sin bloqueos ni OAuth necesario)
        try:
            embed_url = f"https://open.spotify.com/embed/playlist/{clean_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.3"
            }
            res = requests.get(embed_url, headers=headers, timeout=15)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                script = soup.find("script", {"id": "__NEXT_DATA__"})
                if script:
                    data = json.loads(script.string)
                    entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                    track_list = entity.get("trackList", [])
                    for item in track_list:
                        title = item.get("title", "")
                        artist = item.get("subtitle", "")
                        uri = item.get("uri", "")
                        sp_id = uri.split(":")[-1] if ":" in uri else uri
                        tracks.append({
                            "spotify_id": sp_id,
                            "title": title,
                            "artist": artist,
                            "all_artists": artist,
                            "album": "",
                            "album_artist": artist,
                            "release_date": "",
                            "isrc": ""
                        })
                    if tracks:
                        return tracks
        except Exception as e:
            print(f"        [Error en extractor web] {e}")

        if not tracks:
            raise Exception(f"No se pudieron extraer las canciones de Spotify para la playlist {clean_id}.")

        return tracks

# ==============================================================================
# CLIENTE APPLE MUSIC
# ==============================================================================

class AppleMusicAuthError(Exception):
    """Lanzada cuando el token o las cookies de Apple Music han expirado."""
    pass

class AppleMusicClient:
    def __init__(self, token: str, media_user_token: str, cookies: str, storefront: str):
        if not token or not media_user_token:
            raise AppleMusicAuthError("Faltan credenciales de Apple Music (token o media_user_token) en el .env o archivos .dat.")
        self.storefront = storefront or "US"
        self.session = requests.Session()
        headers = {
            "Authorization": token,
            "media-user-token": media_user_token,
            "Host": "amp-api.music.apple.com",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://music.apple.com/",
            "Origin": "https://music.apple.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.3"
        }
        if cookies:
            headers["Cookie"] = cookies.encode("utf-8")
        self.session.headers.update(headers)

    def validate_session(self):
        """Verifica si la sesión actual con Apple Music sigue siendo válida."""
        url = "https://amp-api.music.apple.com/v1/me/library/playlists?limit=1"
        try:
            res = self.session.get(url, timeout=10)
            if res.status_code in (401, 403):
                raise AppleMusicAuthError(
                    f"Error de autenticación con Apple Music ({res.status_code}): Los tokens o cookies han expirado.\n"
                    "Por favor inicia sesión en music.apple.com y actualiza APPLE_MUSIC_MEDIA_USER_TOKEN y APPLE_MUSIC_COOKIES en tu .env"
                )
            elif res.status_code != 200:
                print(f"[Apple Music] Advertencia en comprobación de estado: {res.status_code} - {res.text}")
        except requests.RequestException as e:
            print(f"[Apple Music] Error de conexión al validar sesión: {e}")

    def get_or_create_playlist(self, name: str, description: str = "Sincronizada automáticamente desde Spotify") -> str:
        """Busca una playlist por nombre con paginación completa; si no existe, la crea."""
        url = "https://amp-api.music.apple.com/v1/me/library/playlists"
        target_name_clean = name.strip().lower()

        # 1. Buscar en playlists existentes (con paginación)
        next_url = url
        while next_url:
            res = self.session.get(next_url, timeout=15)
            if res.status_code in (401, 403):
                raise AppleMusicAuthError(f"Error {res.status_code} al listar playlists de Apple Music. Tokens expirados.")
            if res.status_code != 200:
                break

            payload = res.json()
            for pl in payload.get("data", []):
                pl_name = pl.get("attributes", {}).get("name", "").strip().lower()
                if pl_name == target_name_clean:
                    return pl["id"]

            next_url = payload.get("next")
            if next_url and not next_url.startswith("http"):
                next_url = f"https://amp-api.music.apple.com{next_url}"

        # 2. Si no existe, crear la playlist
        create_payload = {
            "attributes": {
                "name": name,
                "description": description
            }
        }
        res = self.session.post(url, json=create_payload, timeout=15)
        if res.status_code == 201:
            data = res.json()
            new_id = data["data"][0]["id"]
            print(f"  [+] Creada nueva playlist en Apple Music: '{name}' (ID: {new_id})")
            time.sleep(0.5)
            return new_id
        else:
            raise Exception(f"No se pudo crear la playlist '{name}' en Apple Music: {res.status_code} - {res.text}")

    def get_playlist_existing_tracks(self, playlist_id: str) -> tuple[set, set]:
        """Obtiene el conjunto de catalogIds y de firmas normalizadas (titulo:::artista) ya presentes en la playlist."""
        catalog_ids = set()
        signatures = set()
        next_url = f"https://amp-api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks?limit=100"

        while next_url:
            res = self.session.get(next_url, timeout=15)
            if res.status_code == 404:
                return set(), set()
            if res.status_code in (401, 403):
                raise AppleMusicAuthError("Tokens de Apple Music expirados al leer canciones de la playlist.")
            if res.status_code != 200:
                break

            data = res.json()
            for track in data.get("data", []):
                attr = track.get("attributes", {})
                play_params = attr.get("playParams", {})
                cid = play_params.get("catalogId") or track.get("id")
                if cid:
                    catalog_ids.add(str(cid))

                name = attr.get("name", "")
                artist = attr.get("artistName", "")
                if name:
                    signatures.add(normalize_signature(name, artist))

            next_url = data.get("next")
            if next_url and not next_url.startswith("http"):
                next_url = f"https://amp-api.music.apple.com{next_url}"

        return catalog_ids, signatures

    def match_by_isrc(self, isrc: str, album: str, album_artist: str) -> str | None:
        """Busca una canción en Apple Music usando el código ISRC."""
        if not isrc:
            return None
        url = f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/songs?filter[isrc]={isrc}"
        try:
            res = self.session.get(url, timeout=10)
            if res.status_code != 200:
                return None
            data = res.json().get("data", [])
            if not data:
                return None

            album_lower = album.lower().strip()
            artist_lower = album_artist.lower().strip()

            for item in data:
                attr = item.get("attributes", {})
                curr_album = attr.get("albumName", "").lower().strip()
                curr_artist = attr.get("artistName", "").lower().strip()

                if curr_album == album_lower and curr_artist == artist_lower:
                    return str(item["id"])
                if curr_album == album_lower or (artist_lower and artist_lower in curr_artist):
                    return str(item["id"])

            # Si hay un único resultado por ISRC, suele ser la misma grabación
            return str(data[0]["id"])
        except Exception:
            return None

    def match_song(self, title: str, artist: str, album: str, isrc: str = "") -> str | None:
        """Busca una canción en Apple Music usando la API interna autenticada (sin bloqueos de rate limit)."""
        # 1. Si tenemos ISRC, probar ISRC primero
        if isrc:
            tid = self.match_by_isrc(isrc, album, artist)
            if tid:
                return tid

        # Limpiar títulos de sufijos molestos
        def clean_text(text: str) -> str:
            t = re.sub(r'\s*[\(\[\{].*?[\)\]\}]', '', text)
            t = re.sub(r'\s*-\s*(remaster|live|radio edit|bonus track|single|deluxe).*', '', t, flags=re.IGNORECASE)
            return t.strip()

        clean_t = clean_text(title)
        primary_artist = artist.split(",")[0].split(" feat")[0].split(" Feat")[0].strip()

        search_terms = [
            f"{clean_t} {primary_artist}",
            f"{title} {artist}",
            f"{clean_t} {artist}",
            clean_t
        ]

        title_l = title.lower().strip()
        clean_t_l = clean_t.lower().strip()
        artist_l = artist.lower().strip()
        p_artist_l = primary_artist.lower().strip()

        # Usar la API oficial autenticada de Apple Music para el catálogo
        catalog_search_url = f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/search"

        for term in set(search_terms):
            if not term:
                continue
            try:
                params = {"types": "songs", "limit": 10, "term": term}
                res = self.session.get(catalog_search_url, params=params, timeout=10)
                if res.status_code == 200:
                    songs = res.json().get("results", {}).get("songs", {}).get("data", [])
                    for item in songs:
                        attr = item.get("attributes", {})
                        t_name = attr.get("name", "").lower().strip()
                        a_name = attr.get("artistName", "").lower().strip()
                        t_clean = clean_text(t_name).lower().strip()

                        # Coincidencia de título y artista
                        if (t_name == title_l or t_clean == clean_t_l) and (a_name == artist_l or p_artist_l in a_name or a_name in p_artist_l):
                            return str(item["id"])
                        if (clean_t_l in t_name or t_name in clean_t_l) and (p_artist_l in a_name or a_name in p_artist_l):
                            return str(item["id"])
            except Exception:
                pass
            time.sleep(0.15)

        return None

    def fetch_equivalent_id(self, catalog_id: str) -> str:
        """Obtiene el ID equivalente en la región configurada para evitar bloqueos regionales."""
        try:
            url = f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/songs?filter[equivalents]={catalog_id}"
            res = self.session.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json().get("data", [])
                if data:
                    return str(data[0]["id"])
        except Exception:
            pass
        return str(catalog_id)

    def add_tracks(self, playlist_id: str, track_ids: list, delay: float = 0.25) -> int:
        """Agrega canciones a la playlist de Apple Music de manera individual y garantizada."""
        if not track_ids:
            return 0

        added_count = 0
        url = f"https://amp-api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks"

        for idx, tid in enumerate(track_ids, start=1):
            payload = {"data": [{"id": str(tid), "type": "songs"}]}
            try:
                res = self.session.post(url, json=payload, timeout=12)
                if res.status_code in (200, 201, 204):
                    added_count += 1
                elif res.status_code in (401, 403):
                    raise AppleMusicAuthError("Token de Apple Music caducó durante la adición de canciones.")
                else:
                    print(f"    [Advertencia] No se pudo añadir pista {tid}: {res.status_code} - {res.text}")
            except AppleMusicAuthError:
                raise
            except Exception as e:
                print(f"    [Error] Fallo al añadir {tid}: {e}")

            time.sleep(delay)

        return added_count

def normalize_signature(title: str, artist: str) -> str:
    """Genera una firma normalizada y limpia (titulo:::artista) para deduplicación infalible."""
    def clean(s: str) -> str:
        s = s.lower().strip()
        # Quitar paréntesis, corchetes y su contenido si contiene feat, remaster, live, etc.
        s = re.sub(r'\s*[\(\[\{].*?(remaster|live|version|edit|feat|bonus|single|deluxe|explicit).*?[\)\]\}]', '', s, flags=re.I)
        # Quitar sufijos comunes
        s = re.sub(r'\s*-\s*(remaster|live|single|deluxe|radio edit|original mix).*', '', s, flags=re.I)
        # Quitar puntuación y espacios extras
        s = re.sub(r'[^\w\s]', '', s)
        return ' '.join(s.split())

    c_title = clean(title)
    # Extraer artista principal
    primary_artist = artist.split(",")[0].split(" feat")[0].split(" Feat")[0].split(" &")[0].strip()
    c_artist = clean(primary_artist)
    return f"{c_title}:::{c_artist}"

class SyncHistoryManager:
    """Gestiona el registro persistente de canciones sincronizadas y no encontradas."""
    def __init__(self, playlist_name: str, cache_dir: str = "sync_history", reset_not_found: bool = False):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        # Generar nombre de archivo limpio a partir de la playlist
        clean_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', playlist_name.lower().strip())
        self.state_file = os.path.join(self.cache_dir, f"{clean_slug}_state.json")
        self.synced_txt_file = os.path.join(self.cache_dir, f"{clean_slug}_synced.txt")
        self.not_found_txt_file = os.path.join(self.cache_dir, f"{clean_slug}_not_found.txt")

        self.state = self._load_state()

        if reset_not_found:
            self.state["not_found"] = {}
            print(f"        [*] Se reinició la lista de canciones no encontradas para '{playlist_name}'.")

    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"synced": {}, "not_found": {}}

    def is_synced(self, sp_id: str, signature: str) -> bool:
        if sp_id and sp_id in self.state["synced"]:
            return True
        for item in self.state["synced"].values():
            if item.get("signature") == signature:
                return True
        return False

    def is_not_found(self, sp_id: str, signature: str) -> bool:
        if sp_id and sp_id in self.state["not_found"]:
            return True
        for item in self.state["not_found"].values():
            if item.get("signature") == signature:
                return True
        return False

    def mark_synced(self, sp_id: str, title: str, artist: str, apple_id: str, signature: str = ""):
        self.state["synced"][sp_id] = {
            "title": title,
            "artist": artist,
            "apple_id": str(apple_id),
            "signature": signature or normalize_signature(title, artist),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if sp_id in self.state["not_found"]:
            del self.state["not_found"][sp_id]

    def mark_not_found(self, sp_id: str, title: str, artist: str, signature: str = ""):
        self.state["not_found"][sp_id] = {
            "title": title,
            "artist": artist,
            "signature": signature or normalize_signature(title, artist),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def save(self):
        """Guarda el archivo JSON y reescribe los archivos .txt legibles para el usuario."""
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)

            with open(self.synced_txt_file, "w", encoding="utf-8") as f:
                f.write(f"# CANCIONES SINCRONIZADAS CON ÉXITO: {len(self.state['synced'])}\n")
                f.write(f"# Última actualización: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("# Formato: [Spotify ID] Título - Artista => Apple Music ID\n\n")
                for sp_id, data in self.state["synced"].items():
                    f.write(f"[{sp_id}] {data['title']} - {data['artist']} => {data.get('apple_id', '')}\n")

            with open(self.not_found_txt_file, "w", encoding="utf-8") as f:
                f.write(f"# CANCIONES NO ENCONTRADAS EN APPLE MUSIC: {len(self.state['not_found'])}\n")
                f.write(f"# Última actualización: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("# (El script las omite automáticamente en cada ejecución para no perder tiempo)\n")
                f.write("# Formato: [Spotify ID] Título - Artista\n\n")
                for sp_id, data in self.state["not_found"].items():
                    f.write(f"[{sp_id}] {data['title']} - {data['artist']}\n")
        except Exception as e:
            print(f"[Aviso] No se pudo guardar historial de sincronización: {e}")

def parse_playlists_config(raw_str: str) -> list:
    """Parsea una lista de cadenas con formato 'SPOTIFY_ID=APPLE_NAME;...'"""
    playlists = []
    if not raw_str:
        return playlists
    entries = raw_str.split(";")
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            sp, ap = entry.split("=", 1)
            playlists.append({"spotify_id": sp.strip(), "apple_name": ap.strip()})
        else:
            playlists.append({"spotify_id": entry.strip(), "apple_name": ""})
    return playlists

def sync_playlist(spotify: SpotifyClient, apple: AppleMusicClient, spotify_id: str, apple_name: str, reset_not_found: bool = False) -> dict:
    """Ejecuta la sincronización incremental de una playlist con caché persistente de sincronizadas y no encontradas."""
    meta = spotify.get_playlist_metadata(spotify_id)
    playlist_name = apple_name or meta.get("name", "Spotify Sync")
    print(f"\n==================================================================")
    print(f"▶ Sincronizando: '{meta.get('name')}' -> Apple Music: '{playlist_name}'")
    print(f"==================================================================")

    history = SyncHistoryManager(playlist_name, reset_not_found=reset_not_found)

    # 1. Obtener canciones de Spotify
    print("  [1/4] Leyendo canciones de Spotify...")
    sp_tracks = spotify.get_all_tracks(spotify_id)
    print(f"        Total en Spotify: {len(sp_tracks)} canciones.")

    # 2. Obtener playlist y canciones ya presentes en Apple Music
    print("  [2/4] Verificando biblioteca de Apple Music...")
    apple_playlist_id = apple.get_or_create_playlist(playlist_name)
    existing_catalog_ids, existing_signatures = apple.get_playlist_existing_tracks(apple_playlist_id)
    print(f"        Canciones actuales en Apple Music: {len(existing_signatures)} (IDs únicos: {len(existing_catalog_ids)})")

    # 3. Emparejar canciones de Spotify que no estén aún en Apple Music ni descartadas previamente
    print("  [3/4] Identificando canciones nuevas...")
    new_track_items_to_add = []
    already_synced_count = 0
    skipped_not_found_count = 0
    newly_not_found_count = 0

    for idx, t in enumerate(sp_tracks, start=1):
        sp_id = t["spotify_id"]
        sig = normalize_signature(t["title"], t["artist"])

        # 1. Si ya está sincronizada (en historial local o directamente en Apple Music) -> SALTAR
        if history.is_synced(sp_id, sig) or sig in existing_signatures:
            already_synced_count += 1
            if not history.is_synced(sp_id, sig):
                history.mark_synced(sp_id, t["title"], t["artist"], "EXISTING", sig)
            continue

        # 2. Si ya fue buscada antes y sabemos que NO existe en Apple Music -> SALTAR INMEDIATO
        if history.is_not_found(sp_id, sig):
            skipped_not_found_count += 1
            continue

        # 3. Solo si es una canción verdaderamente nueva, buscarla en Apple Music
        track_id = apple.match_song(t["title"], t["artist"], t.get("album", ""), t.get("isrc", ""))

        if track_id:
            clean_id = apple.fetch_equivalent_id(track_id)
            if clean_id in existing_catalog_ids:
                already_synced_count += 1
                existing_signatures.add(sig)
                history.mark_synced(sp_id, t["title"], t["artist"], clean_id, sig)
            else:
                new_track_items_to_add.append((clean_id, sp_id, t["title"], t["artist"], sig))
                existing_catalog_ids.add(clean_id)
                existing_signatures.add(sig)
                print(f"        + [Encontrada] Nueva: {t['title']} - {t['artist']} (ID: {clean_id})")
        else:
            newly_not_found_count += 1
            history.mark_not_found(sp_id, t["title"], t["artist"], sig)
            print(f"        ? [No encontrada]: {t['title']} - {t['artist']} (guardada en {history.not_found_txt_file})")

        time.sleep(0.1)

    print(f"\n  Resumen previo:")
    print(f"  - Ya estaban sincronizadas:             {already_synced_count}")
    print(f"  - Omitidas (no existen en Apple Music): {skipped_not_found_count + newly_not_found_count}")
    print(f"  - Nuevas por agregar ahora:             {len(new_track_items_to_add)}")

    # 4. Añadir las canciones nuevas a Apple Music
    added_count = 0
    if new_track_items_to_add:
        print(f"\n  [4/4] Agregando {len(new_track_items_to_add)} canciones a Apple Music...")
        c_ids = [item[0] for item in new_track_items_to_add]
        added_count = apple.add_tracks(apple_playlist_id, c_ids)
        for clean_id, sp_id, title, artist, sig in new_track_items_to_add[:added_count]:
            history.mark_synced(sp_id, title, artist, clean_id, sig)
        print(f"        ¡Éxito! Se añadieron {added_count} canciones.")
    else:
        print("\n  [4/4] Nada nuevo por agregar. La playlist está al día.")

    # Guardar historial y reescribir los archivos .txt legibles para el usuario
    history.save()
    print(f"  [✓] Listas actualizadas en:")
    print(f"      - Sincronizadas:   {history.synced_txt_file}")
    print(f"      - No encontradas:  {history.not_found_txt_file}")

    return {
        "playlist": playlist_name,
        "total_spotify": len(sp_tracks),
        "already_synced": already_synced_count,
        "added": added_count,
        "not_found": skipped_not_found_count + newly_not_found_count
    }

def run_sync_job(config: dict, specific_spotify_id: str = None, specific_apple_name: str = None, reset_not_found: bool = False):
    """Ejecuta una ronda completa de sincronización para todas las playlists configuradas."""
    spotify = SpotifyClient(
        config["spotify_client_id"],
        config["spotify_client_secret"],
        config.get("spotify_refresh_token", "")
    )
    apple = AppleMusicClient(
        config["apple_token"],
        config["apple_media_user_token"],
        config["apple_cookies"],
        config["apple_storefront"]
    )

    # Validar sesión de Apple Music antes de empezar
    apple.validate_session()

    # Determinar qué playlists sincronizar
    targets = []
    if specific_spotify_id:
        targets.append({"spotify_id": specific_spotify_id, "apple_name": specific_apple_name or ""})
    else:
        targets = parse_playlists_config(config["playlists_env"])
        if not targets and os.path.exists("playlists.json"):
            with open("playlists.json", "r", encoding="utf-8") as f:
                targets = json.load(f)

    if not targets:
        print("[!] No se definieron playlists para sincronizar. Configura PLAYLISTS en tu .env o usa --spotify-id.")
        return

    report_lines = []
    for item in targets:
        sp_id = item["spotify_id"]
        ap_name = item.get("apple_name", "")
        res = sync_playlist(spotify, apple, sp_id, ap_name, reset_not_found=reset_not_found)
        report_lines.append(f"• **{res['playlist']}**: +{res['added']} añadidas ({res['already_synced']} ya existían, {res['not_found']} no encontradas).")

    summary = "\n".join(report_lines)
    print(f"\n[OK] Ronda de sincronización finalizada exitosamente a las {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.")
    notify(config["discord_webhook"], "Sincronización Spotify ➜ Apple Music completada", summary, is_error=False)

# ==============================================================================
# PUNTO DE ENTRADA CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Spotify to Apple Music Sync Tool")
    parser.add_argument("--once", action="store_true", help="Ejecuta la sincronización una sola vez y termina (ideal para cron).")
    parser.add_argument("--daemon", "--loop", action="store_true", help="Ejecuta en bucle continuo según el intervalo definido.")
    parser.add_argument("--interval", type=int, default=None, help="Segundos de espera entre sincronizaciones en modo bucle (default: 3600).")
    parser.add_argument("--spotify-id", type=str, default=None, help="ID o URL de una playlist de Spotify específica.")
    parser.add_argument("--apple-name", type=str, default=None, help="Nombre de la playlist de destino en Apple Music.")
    parser.add_argument("--reset-not-found", action="store_true", help="Vuelve a intentar buscar las canciones marcadas anteriormente como no encontradas.")
    args = parser.parse_args()

    config = load_config()

    if args.interval:
        config["sync_interval"] = args.interval

    # Si se especifican argumentos de playlist directamente
    if args.spotify_id:
        try:
            run_sync_job(config, args.spotify_id, args.apple_name, reset_not_found=args.reset_not_found)
        except AppleMusicAuthError as e:
            print(f"\n[ALERTA CRÍTICA] {e}")
            notify(config["discord_webhook"], "⚠️ Tokens de Apple Music expirados", str(e), is_error=True)
            sys.exit(1)
        except Exception as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
        return

    # Modo Daemon (Bucle continuo)
    if args.daemon:
        print(f"[*] Iniciando Spotify-2-AppleMusic en modo Daemon (cada {config['sync_interval']} segundos)...")
        while True:
            try:
                run_sync_job(config, reset_not_found=args.reset_not_found)
            except AppleMusicAuthError as e:
                print(f"\n[ALERTA CRÍTICA] {e}")
                notify(config["discord_webhook"], "⚠️ Tokens de Apple Music expirados en el servidor", str(e), is_error=True)
                print(f"Pausando sincronización por 1 hora antes de reintentar...")
                time.sleep(3600)
            except Exception as e:
                print(f"[ERROR inesperado] {e}")

            print(f"Esperando {config['sync_interval']} segundos hasta la siguiente revisión...\n")
            time.sleep(config["sync_interval"])
    else:
        # Modo único por defecto
        try:
            run_sync_job(config, reset_not_found=args.reset_not_found)
        except AppleMusicAuthError as e:
            print(f"\n[ALERTA CRÍTICA] {e}")
            notify(config["discord_webhook"], "⚠️ Tokens de Apple Music expirados", str(e), is_error=True)
            sys.exit(1)
        except Exception as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
