import subprocess
import os
import tkinter as tk
from tkinter import filedialog, messagebox

def convertir_mp4_a_mp3():
    root = tk.Tk()
    root.withdraw()  # oculta ventana principal

    # 1. Seleccionar archivos MP4
    archivos = filedialog.askopenfilenames(
        title="Selecciona archivos MP4",
        filetypes=[("Archivos MP4", "*.mp4")]
    )

    if not archivos:
        messagebox.showwarning("Cancelado", "No se seleccionaron archivos.")
        return

    # 2. Seleccionar carpeta de destino
    carpeta_destino = filedialog.askdirectory(
        title="Selecciona carpeta de destino para los MP3"
    )

    if not carpeta_destino:
        messagebox.showwarning("Cancelado", "No se seleccionó carpeta de destino.")
        return

    # Conversión
    for archivo in archivos:
        nombre_base = os.path.splitext(os.path.basename(archivo))[0]
        salida_mp3 = os.path.join(carpeta_destino, f"{nombre_base}.mp3")

        subprocess.run([
            "ffmpeg",
            "-i", archivo,
            "-vn",
            "-ab", "192k",
            "-ar", "44100",
            "-y",
            salida_mp3
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    messagebox.showinfo("Listo", "Conversión completada.")

if __name__ == "__main__":
    convertir_mp4_a_mp3()
