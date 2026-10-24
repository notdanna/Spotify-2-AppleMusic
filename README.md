# Spotify to Apple Music

Based on the work of [@simonschellaert](https://github.com/simonschellaert/spotify2am), [@therealmarius](https://github.com/therealmarius) and [@nf1973](https://github.com/nf1973).

Sincroniza e importa tus playlists de Spotify a Apple Music **completamente gratis** (sin necesidad de pagar el Apple Developer Program).

---

## 🚀 Métodos Disponibles

1. **Modo Automático / Servidor 24/7 (`sync.py`)**: Se conecta directamente a la API de Spotify, detecta canciones nuevas de forma incremental y las añade a Apple Music sin duplicados. Diseñado para ejecutarse como `cron` o servicio en segundo plano.
2. **Modo Manual con CSV (`convertsongs.py`)**: Para importar listas puntuales a partir de un archivo `.csv` descargado con [Exportify](https://watsonbox.github.io/exportify/).

---

## 🛠 Modo Automático / Servidor (`sync.py`)

### 1. Requisitos
* Python 3.9+
* Cuenta de Spotify (cualquiera, gratuita o premium).
* Suscripción activa a Apple Music.

Instala las dependencias:
```bash
pip install -r requirements.txt
```

### 2. Configurar Spotify API (100% Gratuito y Permanente)
1. Ve a [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) e inicia sesión.
2. Haz clic en **Create App**:
   * App name: `Spotify-Sync`
   * App description: `Sync tool`
   * Redirect URI: `http://localhost:8888/callback` (no importa para client credentials).
   * Which API/SDKs are you planning to use: marca **Web API**.
3. En la pestaña de configuración de la app, copia tu **Client ID** y **Client Secret**.

### 3. Extraer Credenciales de Apple Music (Sin Apple Developer Program)
1. Abre [music.apple.com](https://music.apple.com) en tu navegador e inicia sesión.
2. Abre las herramientas de desarrollador (**F12** o **Cmd + Option + I** en Mac).
3. Ve a la pestaña **Network (Red)** y recarga la página o interactúa con alguna canción.
4. Filtra por peticiones a `amp-api.music.apple.com` o `buy.music.apple.com`.
5. En los **Request Headers** (Encabezados de la petición), copia:
   * `Authorization` (incluyendo la palabra `Bearer `).
   * `media-user-token`.
   * `Cookie`.

### 4. Configurar el archivo `.env`
Copia el archivo de ejemplo:
```bash
cp .env.example .env
```

Edita `.env` con tus credenciales:
```env
SPOTIFY_CLIENT_ID="tu_spotify_client_id"
SPOTIFY_CLIENT_SECRET="tu_spotify_client_secret"

APPLE_MUSIC_TOKEN="Bearer eyJhbGciOi..."
APPLE_MUSIC_MEDIA_USER_TOKEN="tu_media_user_token"
APPLE_MUSIC_COOKIES="tu_cookie_completa"
APPLE_MUSIC_STOREFRONT="MX"   # MX, ES, US, AR, etc.

# Playlists a sincronizar (ID o URL de Spotify = Nombre en Apple Music)
PLAYLISTS="37i9dQZF1DXcBWIGoYBM5M=Mi Playlist Favorita; https://open.spotify.com/playlist/xyz=Mis Éxitos"

# Opcional: Intervalo en segundos para el modo daemon (3600 = 1 hora)
SYNC_INTERVAL=3600

# Opcional: Webhook de Discord para avisos
DISCORD_WEBHOOK_URL=""
```

### 5. Formas de Ejecutarlo en tu Servidor

#### Opción A: Prueba manual / Sincronización puntual
```bash
python3 sync.py --once
```
O indicando una playlist específica directamente:
```bash
python3 sync.py --spotify-id "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M" --apple-name "Mi Playlist"
```

#### Opción B: Con un Cron Job en Linux/macOS
Para ejecutar la sincronización automáticamente cada día a las 3:00 AM:
```bash
crontab -e
```
Agrega la siguiente línea (ajustando tus rutas):
```cron
0 3 * * * cd /ruta/a/Spotify-2-AppleMusic && /ruta/a/Spotify-2-AppleMusic/.venv/bin/python3 sync.py --once >> sync.log 2>&1
```

#### Opción C: Modo Servicio / Daemon en segundo plano
Corre continuamente revisando cambios cada hora (o según `SYNC_INTERVAL`):
```bash
python3 sync.py --daemon
```

Para dejarlo en segundo plano con `nohup` o `screen`:
```bash
nohup python3 sync.py --daemon > sync.log 2>&1 &
```

> [!IMPORTANT]
> **Caducidad de tokens de Apple Music**:
> Como no se utiliza una clave oficial de Apple Developer ($99/año), el `media-user-token` y las cookies de Apple Music pueden expirar después de varios días o semanas.
> Cuando esto ocurra, el script te avisará por consola (o por Discord Webhook si lo configuraste) con el mensaje de que la sesión caducó. Solo tendrás que volver a copiar los tokens desde `music.apple.com` y pegarlos en el `.env`.

---

## 📄 Modo Manual con CSV (`convertsongs.py`)

Si prefieres el método original importando un archivo CSV:
1. Exporta tu playlist en CSV desde [Exportify](https://watsonbox.github.io/exportify/).
2. Ejecuta:
```bash
python3 convertsongs.py tu_playlist.csv
```

---

##### Licencia: Apache License 2.0
