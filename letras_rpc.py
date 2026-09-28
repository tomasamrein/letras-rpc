"""
Letras RPC — motor
Muestra en tu perfil de Discord (Rich Presence) la línea de letra
que está sonando en Spotify, sincronizada.

- Lee la canción y el segundo actual desde el reproductor de Windows (sin API de Spotify).
- Busca la letra sincronizada en LRCLIB (gratis, sin clave).
- Usa Rich Presence oficial (IPC local + tu propia app de Discord). NO usa tu token.
- Todas las llamadas a Discord (update y clear) pasan por un limitador de frecuencia.

Se puede usar de dos formas:
- Con ventana: ejecutá `app.py` (o el .exe).
- Por consola: `python letras_rpc.py` con la variable LETRAS_RPC_CLIENT_ID.
"""

import asyncio
import bisect
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from pypresence import ActivityType, Presence, StatusDisplayType

__version__ = "1.3.0"

# ---------------- VALORES POR DEFECTO ----------------
MIN_SEGUNDOS_ENTRE_UPDATES = 5  # Discord permite ~5 cambios cada 20 s. Nunca menos de 5.
POLL_SEG = 1.0                  # cada cuánto se mira el reproductor (local, no llama a Discord)

CONFIG_POR_DEFECTO = {
    "client_id": "",
    "offset": 0.0,               # positivo = letra más adelantada, negativo = más atrasada
    "lista_miembros": True,      # True: en la lista de miembros se ve "Escuchando <letra>"
    "portada": True,             # True: muestra la portada del álbum como imagen
}
# -----------------------------------------------------

LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
USER_AGENT = f"LetrasRPC/{__version__} (https://github.com/tomasamrein/letras-rpc)"
SIN_LINEA = "♪ ♪"  # Discord exige al menos 2 caracteres en details/state
ICONO_URL = "https://raw.githubusercontent.com/tomasamrein/letras-rpc/main/assets/icono.png"
MAX_URL_IMAGEN = 256  # Discord no acepta URLs de imagen más largas


# ---------------------------------------------------------------- letras

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
    except (requests.RequestException, ValueError):
        pass
    return None


# ---------------------------------------------------------------- portadas

def _normalizar(texto):
    return re.sub(r"[^a-z0-9]", "", (texto or "").lower())


def _coincide(a, b):
    a, b = _normalizar(a), _normalizar(b)
    return bool(a and b) and (a in b or b in a)


def _portada_itunes(titulo, artista):
    r = requests.get(
        "https://itunes.apple.com/search",
        params={"term": f"{artista} {titulo}", "entity": "song", "limit": 5},
        headers={"User-Agent": USER_AGENT},
        timeout=6,
    )
    if r.status_code != 200:
        return None
    resultados = r.json().get("results", [])
    # Preferir el resultado del mismo artista; si no hay, ninguno (evita portadas equivocadas)
    for item in resultados:
        url = item.get("artworkUrl100")
        if url and _coincide(item.get("artistName"), artista):
            return url.replace("100x100bb", "512x512bb")
    return None


def _portada_deezer(titulo, artista):
    r = requests.get(
        "https://api.deezer.com/search",
        params={"q": f'artist:"{artista}" track:"{titulo}"', "limit": 5},
        headers={"User-Agent": USER_AGENT},
        timeout=6,
    )
    if r.status_code != 200:
        return None
    for item in r.json().get("data", []) or []:
        album = item.get("album") or {}
        url = album.get("cover_xl") or album.get("cover_big")
        if url and _coincide((item.get("artist") or {}).get("name"), artista):
            return url
    return None


def buscar_portada(titulo, artista):
    """URL de la portada del álbum (iTunes y, si no, Deezer). None si no la encuentra."""
    for fuente in (_portada_itunes, _portada_deezer):
        try:
            url = fuente(titulo, artista)
            if url and url.startswith("https://") and len(url) <= MAX_URL_IMAGEN:
                return url
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            continue
    return None


def recortar(texto, limite=120):
    """Discord acepta entre 2 y 128 caracteres."""
    texto = (texto or "").strip()
    if len(texto) < 2:
        return SIN_LINEA
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


# ---------------------------------------------------------------- reproductor (Windows)

class Reproductor:
    """Lee qué suena en Spotify desde los controles multimedia de Windows."""

    def __init__(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
        )
        self._SessionManager = SessionManager
        self._PLAYING = PlaybackStatus.PLAYING
        self._loop = asyncio.new_event_loop()

    async def _leer(self):
        manager = await self._SessionManager.request_async()
        for sesion in manager.get_sessions():
            if "spotify" not in (sesion.source_app_user_model_id or "").lower():
                continue
            props = await sesion.try_get_media_properties_async()
            timeline = sesion.get_timeline_properties()
            sonando = sesion.get_playback_info().playback_status == self._PLAYING

            posicion = timeline.position.total_seconds()
            if sonando:
                ahora = datetime.now(timezone.utc)
                posicion += (ahora - timeline.last_updated_time).total_seconds()

            return {
                "titulo": props.title,
                "artista": props.artist,
                "album": getattr(props, "album_title", "") or "",
                "duracion": timeline.end_time.total_seconds(),
                "posicion": max(posicion, 0.0),
                "sonando": sonando,
            }
        return None

    def leer(self):
        try:
            return self._loop.run_until_complete(self._leer())
        except Exception:  # puede fallar un instante al cambiar de tema
            return None

    def cerrar(self):
        try:
            self._loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Discord

class Discord:
    """Envuelve pypresence: limita la frecuencia de TODAS las llamadas y reconecta con espera."""

    def __init__(self, client_id, detener, reportar):
        self.client_id = client_id
        self.detener = detener
        self.reportar = reportar
        self.rpc = None
        self.ultimo_envio = 0.0

    def conectar(self):
        """Intenta conectar hasta lograrlo o hasta que pidan detener. Devuelve True si conectó."""
        espera = 10
        while not self.detener.is_set():
            try:
                self.rpc = Presence(self.client_id)
                self.rpc.connect()
                self.reportar("estado", "Conectado a Discord")
                return True
            except Exception as e:
                self.cerrar()
                texto = str(e) or e.__class__.__name__
                if "Client ID" in texto or "invalid" in texto.lower():
                    self.reportar("error", "Discord rechazó el Application ID. Revisá que esté bien copiado.")
                else:
                    self.reportar("error", f"No encuentro Discord abierto. Reintento en {espera} s…")
                self.detener.wait(espera)
                espera = min(espera * 2, 60)
        return False

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
        self.ultimo_envio = time.monotonic()  # cuenta aunque falle: nunca hay ráfagas
        try:
            accion(self.rpc)
            return True
        except Exception:
            self.reportar("error", "Se perdió la conexión con Discord. Reconectando…")
            self.cerrar()
            self.detener.wait(MIN_SEGUNDOS_ENTRE_UPDATES)
            self.conectar()
            self.ultimo_envio = time.monotonic()
            return False

    def mostrar(self, linea, titulo, artista, lista_miembros,
                portada=None, album="", inicio=None, fin=None):
        extra = {"status_display_type": StatusDisplayType.DETAILS} if lista_miembros else {}
        if inicio is not None and fin is not None and fin > inicio:
            extra["start"], extra["end"] = int(inicio), int(fin)  # barra de progreso
        if portada:
            extra["large_image"] = portada
            extra["large_text"] = recortar(album or titulo)
            extra["small_image"] = ICONO_URL
            extra["small_text"] = "Letras RPC"
        else:
            extra["large_image"] = ICONO_URL
            extra["large_text"] = "Letras RPC"
        return self._enviar(lambda rpc: rpc.update(
            activity_type=ActivityType.LISTENING,
            details=recortar(linea),
            state=recortar(f"{titulo} · {artista}"),
            **extra,
        ))

    def limpiar(self):
        return self._enviar(lambda rpc: rpc.clear())


# ---------------------------------------------------------------- bucle principal

def ejecutar(config, detener, reportar=None, reproductor=None):
    """
    Corre hasta que `detener` (threading.Event) se active.
    `config` se relee en cada vuelta, así que offset y lista_miembros se pueden cambiar en vivo.
    `reportar(tipo, texto)` recibe: "estado", "tema", "linea", "error".
    """
    reportar = reportar or (lambda tipo, texto: print(f"[{tipo}] {texto}"))
    reproductor = reproductor or Reproductor()
    discord = Discord(str(config["client_id"]).strip(), detener, reportar)
    buscador = ThreadPoolExecutor(max_workers=2)
    portadas = {}  # (titulo, artista) -> URL o None

    try:
        if not discord.conectar():
            return

        tema_actual = None   # (titulo, artista)
        letra = None         # (tiempos, lineas) o None
        mostrada = None      # lo último enviado a Discord (None = nada mostrado)
        avisado_pausa = False

        reportar("estado", "Esperando música en Spotify…")
        while not detener.is_set():
            estado = reproductor.leer()

            # Nada sonando o en pausa -> ocultar presencia (respetando el limitador)
            if not estado or not estado["sonando"] or not estado["titulo"]:
                if not avisado_pausa:
                    reportar("estado", "Spotify en pausa o cerrado")
                    reportar("linea", "")
                    avisado_pausa = True
                if mostrada is not None and discord.puede_enviar():
                    if discord.limpiar():
                        mostrada = None
                detener.wait(POLL_SEG)
                continue

            if avisado_pausa:
                reportar("estado", "Mostrando letra en Discord")
                avisado_pausa = False

            # Cambió la canción -> buscar letra y portada a la vez
            tema = (estado["titulo"], estado["artista"])
            if tema != tema_actual:
                tema_actual = tema
                reportar("tema", f"{tema[0]} — {tema[1]}")
                f_letra = buscador.submit(buscar_letra, tema[0], tema[1], estado["duracion"])
                if tema not in portadas:
                    f_portada = buscador.submit(buscar_portada, *tema)
                    portadas[tema] = f_portada.result()
                    if len(portadas) > 200:  # que el caché no crezca sin límite
                        portadas.pop(next(iter(portadas)))
                letra = f_letra.result()
                if not (letra and letra[0]):
                    letra = None
                    reportar("estado", "Esta canción no tiene letra sincronizada")
                else:
                    reportar("estado", "Mostrando letra en Discord")

            if letra:
                linea = linea_actual(letra, estado["posicion"] + float(config.get("offset", 0.0)))
            else:
                linea = "(sin letra sincronizada)"

            portada = portadas.get(tema) if config.get("portada", True) else None
            clave = (tema, linea, portada)
            if clave != mostrada and discord.puede_enviar():
                inicio = time.time() - estado["posicion"]
                fin = inicio + estado["duracion"] if estado["duracion"] > 0 else None
                if discord.mostrar(linea, *tema, bool(config.get("lista_miembros", True)),
                                   portada=portada, album=estado.get("album", ""),
                                   inicio=inicio, fin=fin):
                    mostrada = clave
                    reportar("linea", linea)

            detener.wait(POLL_SEG)
    finally:
        # Al cerrar la conexión, Discord borra la presencia solo: no hace falta otro envío.
        buscador.shutdown(wait=False)
        discord.cerrar()
        reproductor.cerrar()
        reportar("estado", "Apagado")


def main():
    if not sys.platform.startswith("win"):
        sys.exit("Este programa lee el reproductor de Windows. Solo funciona en Windows.")
    client_id = os.environ.get("LETRAS_RPC_CLIENT_ID", "").strip()
    if not client_id.isdigit():
        sys.exit("Falta el Application ID: usá la variable LETRAS_RPC_CLIENT_ID o abrí app.py.")

    config = dict(CONFIG_POR_DEFECTO, client_id=client_id)
    detener = threading.Event()
    print("Ctrl+C para salir.")
    try:
        ejecutar(config, detener)
    except KeyboardInterrupt:
        detener.set()
        print("\nChau!")


if __name__ == "__main__":
    main()
