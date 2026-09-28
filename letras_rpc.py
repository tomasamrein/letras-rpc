"""
Letras RPC
Muestra en tu perfil de Discord (Rich Presence) la línea de letra
que está sonando en Spotify, sincronizada.

- Lee la canción y el segundo actual desde el reproductor de Windows (sin API de Spotify).
- Busca la letra sincronizada en LRCLIB (gratis, sin clave).
- Usa Rich Presence oficial (IPC local + tu propia app de Discord). NO usa tu token.
- Todas las llamadas a Discord (update y clear) pasan por un limitador de frecuencia.
"""

import asyncio
import bisect
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from pypresence import ActivityType, Presence, StatusDisplayType
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

# ---------------- CONFIGURACIÓN ----------------
# Application ID de tu app en https://discord.com/developers/applications
# (New Application -> General Information -> Application ID).
CLIENT_ID = os.environ.get("LETRAS_RPC_CLIENT_ID", "PEGA_ACA_TU_APPLICATION_ID")

MIN_SEGUNDOS_ENTRE_UPDATES = 5   # Discord permite ~5 cambios cada 20 s. No bajar de 5.
OFFSET_LETRA_SEG = 0.0           # positivo = letra más adelantada, negativo = más atrasada
POLL_SEG = 1.0                   # cada cuánto se mira el reproductor (es local, no llama a Discord)
LETRA_EN_LISTA_DE_MIEMBROS = True  # True: en la lista de miembros se ve "Escuchando <letra>"
# -----------------------------------------------

LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
USER_AGENT = "LetrasRPC/1.1 (https://github.com/lrclib/lrclib)"
SIN_LINEA = "♪ ♪"  # Discord exige al menos 2 caracteres en details/state

_winrt_loop = asyncio.new_event_loop()


def parsear_lrc(texto):
    """Devuelve (tiempos, lineas) ordenados. tiempos en segundos."""
    pares = []
    for raw in texto.splitlines():
        m = LRC_LINE.match(raw.strip())
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            pares.append((t, m.group(3).strip()))
    pares.sort(key=lambda p: p[0])
    return [p[0] for p in pares], [p[1] for p in pares]


def linea_actual(letra, posicion):
    """Devuelve la línea que corresponde a `posicion` (segundos)."""
    tiempos, lineas = letra
    idx = bisect.bisect_right(tiempos, posicion) - 1
    if idx < 0:
        return SIN_LINEA
    return lineas[idx] or SIN_LINEA


def buscar_letra(titulo, artista, duracion):
    """Busca letra sincronizada en LRCLIB. Devuelve (tiempos, lineas) o None."""
    headers = {"User-Agent": USER_AGENT}
    try:
        params = {"track_name": titulo, "artist_name": artista}
        if duracion and duracion > 0:
            params["duration"] = int(duracion)
        r = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=8)
        if r.status_code == 200 and r.json().get("syncedLyrics"):
            return parsear_lrc(r.json()["syncedLyrics"])

        # Plan B: búsqueda más flexible
        r = requests.get(
            "https://lrclib.net/api/search",
            params={"track_name": titulo, "artist_name": artista},
            headers=headers,
            timeout=8,
        )
        if r.status_code == 200:
            for item in r.json():
                if item.get("syncedLyrics"):
                    return parsear_lrc(item["syncedLyrics"])
    except (requests.RequestException, ValueError) as e:
        print(f"[letra] error: {e}")
    return None


async def _leer_reproductor():
    manager = await SessionManager.request_async()
    for sesion in manager.get_sessions():
        if "spotify" not in (sesion.source_app_user_model_id or "").lower():
            continue
        props = await sesion.try_get_media_properties_async()
        timeline = sesion.get_timeline_properties()
        sonando = sesion.get_playback_info().playback_status == PlaybackStatus.PLAYING

        posicion = timeline.position.total_seconds()
        if sonando:
            ahora = datetime.now(timezone.utc)
            posicion += (ahora - timeline.last_updated_time).total_seconds()

        return {
            "titulo": props.title,
            "artista": props.artist,
            "duracion": timeline.end_time.total_seconds(),
            "posicion": max(posicion, 0.0),
            "sonando": sonando,
        }
    return None


def leer_reproductor():
    try:
        return _winrt_loop.run_until_complete(_leer_reproductor())
    except Exception as e:  # el reproductor puede fallar un instante al cambiar de tema
        print(f"[reproductor] {e}")
        return None


def recortar(texto, limite=120):
    """Discord acepta entre 2 y 128 caracteres."""
    texto = (texto or "").strip()
    if len(texto) < 2:
        return SIN_LINEA
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


class Discord:
    """Envuelve pypresence: limita la frecuencia de TODAS las llamadas y reconecta con espera."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.rpc = None
        self.ultimo_envio = 0.0
        self.conectar()

    def conectar(self):
        espera = 10
        while True:
            try:
                self.rpc = Presence(self.client_id)
                self.rpc.connect()
                print("[discord] conectado")
                return
            except Exception as e:
                print(f"[discord] no se pudo conectar ({e}). ¿Está Discord abierto? Reintento en {espera} s...")
                time.sleep(espera)
                espera = min(espera * 2, 60)

    def cerrar(self):
        try:
            if self.rpc:
                self.rpc.close()
        except Exception:
            pass
        self.rpc = None

    def puede_enviar(self):
        return (time.monotonic() - self.ultimo_envio) >= MIN_SEGUNDOS_ENTRE_UPDATES

    def _enviar(self, accion):
        """Ejecuta una llamada a Discord. Devuelve True si salió bien."""
        self.ultimo_envio = time.monotonic()  # cuenta aunque falle, para no reintentar en ráfaga
        try:
            accion(self.rpc)
            return True
        except Exception as e:
            print(f"[discord] error: {e}. Reconecto...")
            self.cerrar()
            time.sleep(MIN_SEGUNDOS_ENTRE_UPDATES)
            self.conectar()
            self.ultimo_envio = time.monotonic()
            return False

    def mostrar(self, linea, titulo, artista):
        extra = {}
        if LETRA_EN_LISTA_DE_MIEMBROS:
            extra["status_display_type"] = StatusDisplayType.DETAILS
        return self._enviar(lambda rpc: rpc.update(
            activity_type=ActivityType.LISTENING,
            details=recortar(linea),
            state=recortar(f"{titulo} · {artista}"),
            **extra,
        ))

    def limpiar(self):
        return self._enviar(lambda rpc: rpc.clear())


def main():
    if not sys.platform.startswith("win"):
        sys.exit("Este programa lee el reproductor de Windows. Solo funciona en Windows.")
    if not CLIENT_ID.isdigit():
        sys.exit("Falta el Application ID: editá CLIENT_ID en el script o usá LETRAS_RPC_CLIENT_ID.")

    discord = Discord(CLIENT_ID)

    tema_actual = None   # (titulo, artista)
    letra = None         # (tiempos, lineas) o None
    mostrada = None      # última línea enviada a Discord (None = nada mostrado)

    print("Listo. Poné música en Spotify. Ctrl+C para salir.")
    try:
        while True:
            estado = leer_reproductor()

            # Nada sonando o en pausa -> limpiar presencia (respetando el limitador)
            if not estado or not estado["sonando"] or not estado["titulo"]:
                if mostrada is not None and discord.puede_enviar():
                    if discord.limpiar():
                        mostrada = None
                time.sleep(POLL_SEG)
                continue

            # Cambió la canción -> buscar letra
            tema = (estado["titulo"], estado["artista"])
            if tema != tema_actual:
                tema_actual = tema
                print(f"[tema] {tema[0]} - {tema[1]}")
                letra = buscar_letra(tema[0], tema[1], estado["duracion"])
                print("[letra] encontrada" if letra and letra[0] else "[letra] no encontrada")

            if letra and letra[0]:
                linea = linea_actual(letra, estado["posicion"] + OFFSET_LETRA_SEG)
            else:
                linea = "(sin letra sincronizada)"

            clave = (tema, linea)
            if clave != mostrada and discord.puede_enviar():
                if discord.mostrar(linea, *tema):
                    mostrada = clave

            time.sleep(POLL_SEG)
    except KeyboardInterrupt:
        print("\nChau!")
    finally:
        # Al cerrar la conexión, Discord borra la presencia solo: no hace falta otro envío.
        discord.cerrar()


if __name__ == "__main__":
    main()
