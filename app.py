"""
Letras RPC — ventana
Interfaz simple: pegás tu Application ID, tocás Prender y listo.
"""

import json
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox

import letras_rpc as motor

APP = "LetrasRPC"
PORTAL = "https://discord.com/developers/applications"
REPO = "https://github.com/tomasamrein/letras-rpc"
CARPETA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
ARCHIVO_CONFIG = os.path.join(CARPETA, "config.json")

# Paleta estilo Discord
FONDO = "#1e1f22"
PANEL = "#2b2d31"
CAMPO = "#383a40"
TEXTO = "#f2f3f5"
TENUE = "#b5bac1"
BLURPLE = "#5865f2"
VERDE = "#23a55a"
ROJO = "#da373c"
AMARILLO = "#f0b232"


def ruta_recurso(nombre):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nombre)


# ---------------------------------------------------------------- configuración

def cargar_config():
    config = dict(motor.CONFIG_POR_DEFECTO, auto_prender=False)
    try:
        with open(ARCHIVO_CONFIG, encoding="utf-8") as f:
            config.update(json.load(f))
    except (OSError, ValueError):
        pass
    return config


def guardar_config(config):
    try:
        os.makedirs(CARPETA, exist_ok=True)
        with open(ARCHIVO_CONFIG, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------- Windows

def una_sola_instancia():
    """Evita que haya dos Letras RPC abiertos (duplicaría los envíos a Discord)."""
    if not sys.platform.startswith("win"):
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\LetrasRPC_instancia_unica")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def comando_inicio():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimizado'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --minimizado'


def inicio_con_windows(activar=None):
    """Sin argumento: devuelve si está activado. Con True/False: lo cambia."""
    if not sys.platform.startswith("win"):
        return False
    import winreg
    clave = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            if activar is None:
                try:
                    winreg.QueryValueEx(k, APP)
                    return True
                except FileNotFoundError:
                    return False
            if activar:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, comando_inicio())
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except FileNotFoundError:
                    pass
            return activar
    except OSError:
        return False


# ---------------------------------------------------------------- ventana

class App:
    def __init__(self, root, minimizado=False):
        self.root = root
        self.config = cargar_config()
        self.cola = queue.Queue()
        self.hilo = None
        self.detener = None

        root.title("Letras RPC")
        root.configure(bg=FONDO)
        root.resizable(False, False)
        try:
            root.iconbitmap(ruta_recurso(os.path.join("assets", "icono.ico")))
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.al_cerrar)

        self._armar()
        self._refrescar_boton()
        root.after(150, self._leer_cola)

        if minimizado:
            root.iconify()
        if self.config.get("auto_prender") or minimizado:
            if self._id_valido():
                root.after(300, self.prender)

    # ----------------------------------------------------------- UI

    def _armar(self):
        r = self.root
        fuente = "Segoe UI"

        cab = tk.Frame(r, bg=FONDO)
        cab.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(cab, text="🎤 Letras RPC", font=(fuente, 18, "bold"),
                 bg=FONDO, fg=TEXTO).pack(side="left")
        tk.Label(cab, text=f"v{motor.__version__}", font=(fuente, 9),
                 bg=FONDO, fg=TENUE).pack(side="left", padx=8, pady=(8, 0))

        tk.Label(r, text="La letra de lo que escuchás en Spotify, en tu perfil de Discord.",
                 font=(fuente, 10), bg=FONDO, fg=TENUE).pack(anchor="w", padx=20)

        # --- App ID
        caja = self._panel()
        fila = tk.Frame(caja, bg=PANEL)
        fila.pack(fill="x")
        tk.Label(fila, text="APPLICATION ID", font=(fuente, 8, "bold"),
                 bg=PANEL, fg=TENUE).pack(side="left")
        ayuda = tk.Label(fila, text="¿De dónde lo saco?", font=(fuente, 9, "underline"),
                         bg=PANEL, fg=BLURPLE, cursor="hand2")
        ayuda.pack(side="right")
        ayuda.bind("<Button-1>", lambda e: self._ayuda_id())

        self.var_id = tk.StringVar(value=self.config.get("client_id", ""))
        self.entry_id = tk.Entry(caja, textvariable=self.var_id, font=("Consolas", 12),
                                 bg=CAMPO, fg=TEXTO, insertbackground=TEXTO,
                                 relief="flat", highlightthickness=0)
        self.entry_id.pack(fill="x", pady=(6, 0), ipady=7)
        self.var_id.trace_add("write", lambda *a: self._refrescar_boton())

        # --- Botón principal
        self.boton = tk.Button(r, font=(fuente, 13, "bold"), relief="flat", bd=0,
                               fg="white", activeforeground="white", cursor="hand2", highlightthickness=0,
                               command=self.alternar)
        self.boton.pack(fill="x", padx=20, pady=(12, 4), ipady=8)

        # --- Estado en vivo
        vivo = self._panel()
        fila = tk.Frame(vivo, bg=PANEL)
        fila.pack(fill="x")
        self.punto = tk.Label(fila, text="●", font=(fuente, 11), bg=PANEL, fg=TENUE)
        self.punto.pack(side="left")
        self.lbl_estado = tk.Label(fila, text="Apagado", font=(fuente, 10),
                                   bg=PANEL, fg=TENUE, anchor="w")
        self.lbl_estado.pack(side="left", padx=6, fill="x")

        self.lbl_tema = tk.Label(vivo, text="", font=(fuente, 10, "bold"), bg=PANEL,
                                 fg=TEXTO, anchor="w", wraplength=400, justify="left")
        self.lbl_tema.pack(fill="x", pady=(10, 0))
        self.lbl_linea = tk.Label(vivo, text="", font=(fuente, 12, "italic"), bg=PANEL,
                                  fg=TEXTO, anchor="w", wraplength=400, justify="left")
        self.lbl_linea.pack(fill="x", pady=(4, 0))

        # --- Opciones
        opc = self._panel()
        tk.Label(opc, text="OPCIONES", font=(fuente, 8, "bold"),
                 bg=PANEL, fg=TENUE).pack(anchor="w")

        fila = tk.Frame(opc, bg=PANEL)
        fila.pack(fill="x", pady=(6, 2))
        tk.Label(fila, text="Sincronía de la letra (segundos)", font=(fuente, 10),
                 bg=PANEL, fg=TEXTO).pack(side="left")
        self.var_offset = tk.DoubleVar(value=float(self.config.get("offset", 0.0)))
        spin = tk.Spinbox(fila, from_=-10, to=10, increment=0.5, width=5,
                          textvariable=self.var_offset, font=(fuente, 10),
                          bg=CAMPO, fg=TEXTO, buttonbackground=CAMPO, relief="flat",
                          insertbackground=TEXTO, command=self._opciones)
        spin.pack(side="right")
        spin.bind("<KeyRelease>", lambda e: self._opciones())
        tk.Label(opc, text="Si la letra va atrasada, subilo. Si va adelantada, bajalo.",
                 font=(fuente, 8), bg=PANEL, fg=TENUE).pack(anchor="w")

        self.var_miembros = tk.BooleanVar(value=bool(self.config.get("lista_miembros", True)))
        self.var_portada = tk.BooleanVar(value=bool(self.config.get("portada", True)))
        self.var_auto = tk.BooleanVar(value=bool(self.config.get("auto_prender", False)))
        self.var_inicio = tk.BooleanVar(value=inicio_con_windows())
        self._check(opc, "Mostrar la portada del álbum", self.var_portada, self._opciones)
        self._check(opc, "Mostrar la letra en la lista de miembros del server", self.var_miembros, self._opciones)
        self._check(opc, "Prender automáticamente al abrir", self.var_auto, self._opciones)
        self._check(opc, "Abrir con Windows", self.var_inicio, self._cambiar_inicio)

        # --- Pie
        pie = tk.Frame(r, bg=FONDO)
        pie.pack(fill="x", padx=20, pady=(4, 14))
        tk.Label(pie, text="🔒 No usa tu token. Rich Presence oficial.",
                 font=(fuente, 8), bg=FONDO, fg=TENUE).pack(side="left")
        link = tk.Label(pie, text="GitHub", font=(fuente, 8, "underline"),
                        bg=FONDO, fg=BLURPLE, cursor="hand2")
        link.pack(side="right")
        link.bind("<Button-1>", lambda e: webbrowser.open(REPO))

    def _panel(self):
        marco = tk.Frame(self.root, bg=PANEL, padx=14, pady=12)
        marco.pack(fill="x", padx=20, pady=6)
        return marco

    def _check(self, padre, texto, var, comando):
        tk.Checkbutton(padre, text=texto, variable=var, command=comando,
                       font=("Segoe UI", 10), bg=PANEL, fg=TEXTO, selectcolor=CAMPO,
                       activebackground=PANEL, activeforeground=TEXTO,
                       highlightthickness=0, bd=0, anchor="w").pack(fill="x", pady=1)

    # ----------------------------------------------------------- lógica

    def _id_valido(self):
        valor = self.var_id.get().strip()
        return valor.isdigit() and 15 <= len(valor) <= 22

    def _corriendo(self):
        return self.hilo is not None and self.hilo.is_alive()

    def _refrescar_boton(self):
        if self._corriendo():
            self.boton.config(text="■  Apagar", bg=ROJO, activebackground="#a12d31", state="normal")
            self.entry_id.config(state="disabled", disabledbackground=CAMPO, disabledforeground=TENUE)
        else:
            ok = self._id_valido()
            self.boton.config(text="▶  Prender", bg=VERDE if ok else CAMPO,
                              activebackground="#1a7f43", state="normal" if ok else "disabled",
                              disabledforeground=TENUE)
            self.entry_id.config(state="normal")

    def _opciones(self):
        try:
            self.config["offset"] = float(self.var_offset.get())
        except (tk.TclError, ValueError):
            pass
        self.config["lista_miembros"] = self.var_miembros.get()
        self.config["portada"] = self.var_portada.get()
        self.config["auto_prender"] = self.var_auto.get()
        guardar_config(self.config)

    def _cambiar_inicio(self):
        self.var_inicio.set(inicio_con_windows(self.var_inicio.get()))

    def _ayuda_id(self):
        if messagebox.askokcancel(
            "Cómo conseguir tu Application ID",
            "1. Se abre el portal de desarrolladores de Discord.\n"
            "2. Tocá «New Application» y ponele un nombre.\n"
            "   Ese nombre es lo que se ve: «Escuchando <nombre>».\n"
            "3. En «General Information» copiá el «Application ID».\n"
            "4. Pegalo acá y tocá Prender.\n\n"
            "¿Abrir el portal ahora?",
        ):
            webbrowser.open(PORTAL)

    def alternar(self):
        if self._corriendo():
            self.apagar()
        else:
            self.prender()

    def prender(self):
        if self._corriendo() or not self._id_valido():
            return
        self.config["client_id"] = self.var_id.get().strip()
        self._opciones()
        self.detener = threading.Event()
        self.hilo = threading.Thread(
            target=self._correr, args=(self.config, self.detener), daemon=True)
        self.hilo.start()
        self._estado("estado", "Conectando con Discord…")
        self._refrescar_boton()

    def _correr(self, config, detener):
        try:
            motor.ejecutar(config, detener, lambda t, x: self.cola.put((t, x)))
        except Exception as e:  # que un error no mate la ventana
            self.cola.put(("error", f"Error inesperado: {e}"))
        finally:
            self.cola.put(("fin", ""))

    def apagar(self):
        if self.detener:
            self.detener.set()
        self._estado("estado", "Apagando…")
        self.boton.config(state="disabled")

    def _leer_cola(self):
        try:
            while True:
                self._estado(*self.cola.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._leer_cola)

    def _estado(self, tipo, texto):
        if tipo == "fin":
            self.punto.config(fg=TENUE)
            self.lbl_estado.config(text="Apagado", fg=TENUE)
            self.lbl_tema.config(text="")
            self.lbl_linea.config(text="")
            self.root.after(100, self._refrescar_boton)
        elif tipo == "estado":
            color = VERDE if "Mostrando" in texto or "Conectado" in texto else TENUE
            if "sin letra" in texto.lower() or "no tiene" in texto:
                color = AMARILLO
            self.punto.config(fg=color)
            self.lbl_estado.config(text=texto, fg=TEXTO if color != TENUE else TENUE)
        elif tipo == "error":
            self.punto.config(fg=ROJO)
            self.lbl_estado.config(text=texto, fg=TEXTO)
        elif tipo == "tema":
            self.lbl_tema.config(text=f"🎵 {texto}")
            self.lbl_linea.config(text="")
        elif tipo == "linea":
            self.lbl_linea.config(text=f"“{texto}”" if texto else "")

    def al_cerrar(self):
        if self.detener:
            self.detener.set()
        if self.hilo:
            self.hilo.join(timeout=3)
        self.root.destroy()


# ---------------------------------------------------------------- arranque

def autotest():
    """Usado por la compilación automática: verifica que el .exe tenga todo adentro."""
    import pypresence  # noqa: F401
    import requests    # noqa: F401
    rep = motor.Reproductor()  # importa winrt
    rep.leer()
    rep.cerrar()
    assert motor.parsear_lrc("[00:01.00] hola")[1] == ["hola"]
    return 0


def main():
    if "--autotest" in sys.argv:
        # os._exit evita cualquier ventana de error que trabe la compilación automática
        try:
            codigo = autotest()
        except Exception:
            import traceback
            traceback.print_exc(file=open("autotest_error.txt", "w", encoding="utf-8"))
            codigo = 1
        os._exit(codigo)

    if not una_sola_instancia():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("Letras RPC", "Letras RPC ya está abierto.\nBuscalo en la barra de tareas.")
        return

    root = tk.Tk()
    App(root, minimizado="--minimizado" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
