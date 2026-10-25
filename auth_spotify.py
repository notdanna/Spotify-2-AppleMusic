#!/usr/bin/env python3
"""
Spotify One-Time Authorization Tool
Inicia un servidor temporal para autorizar tu cuenta de Spotify y obtener
el SPOTIFY_REFRESH_TOKEN permanente para leer playlists de más de 100 canciones.
"""

import os
import sys
import base64
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8000/callback"
PORT = 8000

if not CLIENT_ID or not CLIENT_SECRET:
    print("[Error] Configura primero SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET en tu archivo .env")
    sys.exit(1)

auth_code = None

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <html>
            <body style="font-family: system-ui; text-align: center; padding: 50px;">
                <h1 style="color: #1DB954;">¡Autorización exitosa!</h1>
                <p>Puedes cerrar esta pestaña y volver a la terminal.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Error: No se recibio codigo de autorizacion.")

    def log_message(self, format, *args):
        # Silenciar logs HTTP
        pass

def exchange_code_for_token(code: str) -> str:
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    res = requests.post("https://accounts.spotify.com/api/token", data=payload, headers=headers, timeout=10)
    if res.status_code != 200:
        raise Exception(f"Fallo al obtener refresh token: {res.text}")

    data = res.json()
    return data.get("refresh_token")

def update_env_file(refresh_token: str):
    env_path = ".env"
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        if line.startswith("SPOTIFY_REFRESH_TOKEN="):
            new_lines.append(f'SPOTIFY_REFRESH_TOKEN="{refresh_token}"\n')
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f'\n# REFRESH TOKEN PERMANENTE DE SPOTIFY\nSPOTIFY_REFRESH_TOKEN="{refresh_token}"\n')

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def main():
    scope = "playlist-read-private playlist-read-collaborative user-library-read"
    auth_url = (
        f"https://accounts.spotify.com/authorize?"
        f"client_id={CLIENT_ID}&"
        f"response_type=code&"
        f"redirect_uri={urllib.parse.quote(REDIRECT_URI)}&"
        f"scope={urllib.parse.quote(scope)}"
    )

    server = HTTPServer(("127.0.0.1", PORT), OAuthCallbackHandler)
    print("\n=======================================================")
    print("🔑 AUTORIZACIÓN DE SPOTIFY (Una sola vez para tu servidor)")
    print("=======================================================")
    print("Abriendo el navegador para autorizar la lectura de playlists...")
    print(f"Si no abre automáticamente, entra a este enlace:\n\n{auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Esperar solo una solicitud
    server.handle_request()

    if auth_code:
        print("[+] Código recibido. Obteniendo Refresh Token...")
        refresh_token = exchange_code_for_token(auth_code)
        if refresh_token:
            update_env_file(refresh_token)
            print(f"[✓] ¡ÉXITO! Tu SPOTIFY_REFRESH_TOKEN fue guardado en el archivo .env.")
            print("[✓] Ahora podrás leer playlists de cualquier tamaño (1,000+ canciones) sin límites.")
        else:
            print("[Error] No se recibió refresh_token en la respuesta de Spotify.")
    else:
        print("[Error] No se pudo capturar el código de autorización.")

if __name__ == "__main__":
    main()
