import os
import re
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

# ---------- Utilidades ffmpeg/ffprobe ----------

def ffprobe_duration_seconds(path: str) -> float:
    """Devuelve duración en segundos usando ffprobe. 0 si falla."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0

def run_ffmpeg_with_progress(infile: str, outfile: str, q: queue.Queue):
    """
    Ejecuta ffmpeg y reporta progreso vía q:
    - ("file_progress", pct, out_time_s, speed, eta_s)
    - ("file_done", ok, err_msg)
    """
    duration = ffprobe_duration_seconds(infile)
    if duration <= 0:
        # si no hay duración, igual intentamos convertir, pero sin % real
        duration = None

    cmd = [
        "ffmpeg",
        "-y",
        "-i", infile,
        "-vn",
        "-ab", "192k",
        "-ar", "44100",
        # progreso por stdout:
        "-progress", "pipe:1",
        "-nostats",
        outfile
    ]

    # Nota: ffmpeg escribe el progreso en stdout (por -progress pipe:1)
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True
    )

    last_out_time = 0.0
    last_speed = None

    try:
        for line in p.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if key == "out_time_ms":
                # out_time_ms viene en microsegundos*1000? en realidad es microsegundos en ms:
                # ffmpeg usa microsegundos en out_time_us a veces. En -progress suele ser out_time_ms (microsegundos en ms).
                # Interpretación segura: si es muy grande, lo convertimos a segundos.
                try:
                    out_time_ms = int(value)
                    out_time_s = out_time_ms / 1_000_000.0  # suele venir en microsegundos (us)
                except ValueError:
                    out_time_s = last_out_time

                last_out_time = out_time_s

                if duration:
                    frac = max(0.0, min(1.0, out_time_s / duration))
                    pct = frac * 100.0
                    eta_s = max(0.0, duration - out_time_s)
                else:
                    pct = 0.0
                    eta_s = None

                q.put(("file_progress", pct, out_time_s, last_speed, eta_s))

            elif key == "speed":
                # speed tipo "1.23x"
                last_speed = value
                q.put(("speed", last_speed))

            elif key == "progress" and value == "end":
                break

        p.wait()

        if p.returncode == 0:
            q.put(("file_done", True, ""))
        else:
            err = p.stderr.read() if p.stderr else ""
            q.put(("file_done", False, err.strip()[:800]))

    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        q.put(("file_done", False, str(e)))

# ---------- UI / App ----------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MP4 → MP3 (ffmpeg)")

        self.q = queue.Queue()
        self.worker = None
        self.cancel = False

        # Estilo más agradable (limitado por plataforma)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Sub.TLabel", font=("Segoe UI", 10))
        style.configure("Info.TLabel", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TProgressbar", thickness=18)

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Convertidor MP4 a MP3", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text="Selecciona tus MP4, elige carpeta destino y mira el progreso real.", style="Sub.TLabel").pack(anchor="w", pady=(2, 12))

        # Archivo actual
        self.lbl_file = ttk.Label(main, text="Archivo: —", style="Info.TLabel")
        self.lbl_file.pack(anchor="w", pady=(0, 4))

        self.pb_file = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.pb_file.pack(fill="x", pady=(0, 4))

        self.lbl_file_stats = ttk.Label(main, text="0%   |   ETA: —   |   Speed: —", style="Info.TLabel")
        self.lbl_file_stats.pack(anchor="w", pady=(0, 10))

        # Global
        ttk.Label(main, text="Progreso total", style="Sub.TLabel").pack(anchor="w")
        self.pb_total = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.pb_total.pack(fill="x", pady=(4, 4))

        self.lbl_total = ttk.Label(main, text="Total: 0% (0/0)", style="Info.TLabel")
        self.lbl_total.pack(anchor="w", pady=(0, 12))

        # Botones
        btns = ttk.Frame(main)
        btns.pack(fill="x", pady=(6, 0))

        self.btn_start = ttk.Button(btns, text="Seleccionar MP4 y convertir", command=self.start_flow)
        self.btn_start.pack(side="left")

        self.btn_cancel = ttk.Button(btns, text="Cancelar", command=self.request_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))

        self.root.resizable(False, False)

        # Estado del trabajo
        self.files = []
        self.dest = ""
        self.total_count = 0
        self.done_count = 0
        self.current_pct = 0.0
        self.current_speed = None
        self.errors = []

        # Loop de UI para leer cola
        self.root.after(60, self.poll_queue)

    def start_flow(self):
        files = filedialog.askopenfilenames(
            title="¿Dónde están los archivos a convertir?",
            filetypes=[("Archivos MP4", "*.mp4")]
        )
        if not files:
            return

        dest = filedialog.askdirectory(
            title="Ubica la carpeta donde se guardarán los convertidos"
        )
        if not dest:
            return

        self.files = list(files)
        self.dest = dest
        self.total_count = len(self.files)
        self.done_count = 0
        self.current_pct = 0.0
        self.current_speed = None
        self.errors = []
        self.cancel = False

        # Reset UI
        self.pb_file.config(value=0)
        self.pb_total.config(value=0)
        self.lbl_total.config(text=f"Total: 0% (0/{self.total_count})")
        self.lbl_file.config(text="Archivo: preparando…")
        self.lbl_file_stats.config(text="0%   |   ETA: —   |   Speed: —")

        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")

        # Lanzar hilo principal de conversión
        self.worker = threading.Thread(target=self.convert_all, daemon=True)
        self.worker.start()

    def request_cancel(self):
        self.cancel = True
        self.btn_cancel.config(state="disabled")
        self.lbl_file.config(text="Cancelando…")

    def convert_all(self):
        for idx, infile in enumerate(self.files, start=1):
            if self.cancel:
                break

            base = os.path.splitext(os.path.basename(infile))[0]
            outfile = os.path.join(self.dest, f"{base}.mp3")

            # Notificar inicio de archivo
            self.q.put(("new_file", idx, self.total_count, os.path.basename(infile)))

            run_ffmpeg_with_progress(infile, outfile, self.q)

            # Esperar resultado file_done para contar correctamente.
            # (Lo recibimos en poll_queue; aquí solo seguimos.)
        self.q.put(("all_done",))

    def poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.handle_msg(msg)
        except queue.Empty:
            pass

        self.root.after(60, self.poll_queue)

    def handle_msg(self, msg):
        kind = msg[0]

        if kind == "new_file":
            idx, total, name = msg[1], msg[2], msg[3]
            self.current_pct = 0.0
            self.current_speed = None
            self.pb_file.config(value=0)
            self.lbl_file.config(text=f"Archivo {idx}/{total}: {name}")
            self.lbl_file_stats.config(text="0%   |   ETA: —   |   Speed: —")

        elif kind == "file_progress":
            pct, out_time_s, speed, eta_s = msg[1], msg[2], msg[3], msg[4]
            self.current_pct = pct

            # Barra y % actual
            self.pb_file.config(value=pct)

            # Speed
            sp = speed if speed else (self.current_speed if self.current_speed else "—")

            # ETA
            if eta_s is None:
                eta_txt = "—"
            else:
                eta_txt = self.format_seconds(eta_s)

            self.lbl_file_stats.config(text=f"{pct:5.1f}%   |   ETA: {eta_txt}   |   Speed: {sp}")

            # Progreso total suave: (done + pct_actual/100) / total
            total_frac = (self.done_count + (pct / 100.0)) / max(1, self.total_count)
            total_pct = total_frac * 100.0
            self.pb_total.config(value=total_pct)
            self.lbl_total.config(text=f"Total: {total_pct:5.1f}% ({self.done_count}/{self.total_count})")

        elif kind == "speed":
            self.current_speed = msg[1]

        elif kind == "file_done":
            ok, err = msg[1], msg[2]
            if ok:
                self.done_count += 1
            else:
                self.done_count += 1
                self.errors.append(err if err else "Error desconocido")

            # Ajuste global al cerrar archivo (100% del tramo)
            total_frac = (self.done_count) / max(1, self.total_count)
            total_pct = total_frac * 100.0
            self.pb_total.config(value=total_pct)
            self.lbl_total.config(text=f"Total: {total_pct:5.1f}% ({self.done_count}/{self.total_count})")

        elif kind == "all_done":
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")

            if self.cancel:
                messagebox.showinfo("Cancelado", "Se canceló la conversión.")
                self.lbl_file.config(text="Archivo: —")
                return

            if self.errors:
                messagebox.showwarning("Terminado con errores", "Algunos archivos fallaron. Revisa ffmpeg/ffprobe y formatos.")
            else:
                messagebox.showinfo("Listo", "Conversión completada.")

            self.lbl_file.config(text="Archivo: —")

    @staticmethod
    def format_seconds(s: float) -> str:
        s = int(max(0, s))
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        if h > 0:
            return f"{h:d}:{m:02d}:{sec:02d}"
        return f"{m:d}:{sec:02d}"

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
