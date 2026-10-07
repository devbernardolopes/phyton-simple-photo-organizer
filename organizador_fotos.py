# -*- coding: utf-8 -*-
"""
Organizador de Fotos por EXIF
=============================

Organiza fotos em subpastas  ANO / MÊS / DIA / HORA  dentro de uma pasta de
destino, usando a data de captura gravada no EXIF. Se a imagem não tiver uma
data EXIF válida, usa a data da última modificação do arquivo.

Os arquivos são copiados/movidos byte a byte (sem recodificar), portanto o EXIF
é preservado integralmente. A data de modificação também é preservada.

O código está dividido em duas partes:
  1. Lógica (sem dependência de interface) - facilita testes e manutenção.
  2. Interface gráfica (Tkinter) - todos os textos em português do Brasil.
"""

from __future__ import annotations

import filecmp
import json
import os
import queue
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import exifread

# =============================================================================
# 1. LÓGICA
# =============================================================================

# Extensões consideradas imagens (comparação sem diferenciar maiúsculas).
# Inclui formatos RAW comuns de Nikon (NEF/NRW) e Canon (CR2/CR3).
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".png", ".heic", ".heif",
    ".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".dng", ".orf",
    ".rw2", ".raf", ".pef", ".srw",
}

# Ordem de prioridade das tags de data no EXIF.
EXIF_DATE_TAGS = (
    "EXIF DateTimeOriginal",   # momento em que a foto foi tirada
    "EXIF DateTimeDigitized",  # momento da digitalização
    "Image DateTime",          # última modificação gravada pelo software/câmera
)
EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
MIN_VALID_YEAR = 1990  # datas anteriores são tratadas como lixo/inválidas

TEMP_SUFFIX = ".tmp-organizador"


@dataclass
class Options:
    origin: str
    dest: str
    move: bool = False
    recursive: bool = True
    dry_run: bool = False


@dataclass
class Stats:
    total: int = 0            # imagens encontradas
    ignored: int = 0          # arquivos que não são imagens
    done: int = 0             # copiadas/movidas (ou que seriam, na simulação)
    duplicates: int = 0       # já existiam no destino com o mesmo conteúdo
    in_place: int = 0         # já estavam na posição correta
    used_mtime: int = 0       # sem data EXIF válida -> data de modificação
    errors: int = 0
    cancelled: bool = False


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


def _parse_exif_date(raw: str) -> Optional[datetime]:
    """Converte 'AAAA:MM:DD HH:MM:SS' em datetime; devolve None se inválido."""
    raw = raw.strip().strip("\x00").strip()
    try:
        dt = datetime.strptime(raw[:19], EXIF_DATE_FORMAT)
    except ValueError:  # inclui '0000:00:00 00:00:00' e textos quebrados
        return None
    if MIN_VALID_YEAR <= dt.year <= datetime.now().year + 1:
        return dt
    return None


def read_exif_datetime(path: str) -> Optional[datetime]:
    """Lê a data de captura do EXIF. Devolve None se ausente ou inválida."""
    try:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
    except Exception:
        return None
    for key in EXIF_DATE_TAGS:
        tag = tags.get(key)
        if tag is None:
            continue
        dt = _parse_exif_date(str(tag))
        if dt is not None:
            return dt
    return None


def get_photo_datetime(path: str) -> tuple[datetime, bool]:
    """Devolve (data, veio_do_exif). Sem EXIF válido, usa a data de modificação."""
    dt = read_exif_datetime(path)
    if dt is not None:
        return dt, True
    return datetime.fromtimestamp(os.path.getmtime(path)), False


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # unidades diferentes no Windows
        return False


def find_images(origin: str, dest: str, recursive: bool) -> tuple[list[str], int]:
    """Lista as imagens da origem. Devolve (imagens, qtd_ignorados).

    A pasta de destino é ignorada caso esteja dentro da origem, para não
    reprocessar fotos já organizadas.
    """
    images: list[str] = []
    ignored = 0
    dest_norm = _norm(dest)

    def consider(path: str) -> None:
        nonlocal ignored
        if os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS:
            images.append(path)
        else:
            ignored += 1

    if recursive:
        for dirpath, dirnames, filenames in os.walk(origin):
            dirnames[:] = [
                d for d in dirnames
                if not _is_inside(_norm(os.path.join(dirpath, d)), dest_norm)
            ]
            dirnames.sort()
            for name in sorted(filenames):
                consider(os.path.join(dirpath, name))
    else:
        with os.scandir(origin) as it:
            for entry in sorted(it, key=lambda e: e.name):
                if entry.is_file():
                    consider(entry.path)
    return images, ignored


def _same_content(a: str, b: str) -> bool:
    """Compara o conteúdo byte a byte (tamanho primeiro, por rapidez)."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def resolve_destination(src: str, folder: str, planned: dict[str, str]) -> tuple[str, str]:
    """Decide o que fazer com `src` ao colocá-lo em `folder`.

    Devolve (status, caminho):
      "new"       -> grave em `caminho` (nome livre; pode ter sufixo _1, _2...)
      "duplicate" -> já existe um arquivo idêntico em `caminho`
      "in_place"  -> o arquivo já está exatamente em `caminho`

    `planned` guarda destinos já "reservados" numa simulação (nada é gravado
    em disco), para que os conflitos entre arquivos sejam detectados também lá.
    """
    base, ext = os.path.splitext(os.path.basename(src))
    n = 0
    while True:
        name = f"{base}{ext}" if n == 0 else f"{base}_{n}{ext}"
        candidate = os.path.join(folder, name)
        key = os.path.normcase(candidate)
        if key in planned:
            other = planned[key]
        elif os.path.exists(candidate):
            try:
                if os.path.samefile(src, candidate):
                    return "in_place", candidate
            except OSError:
                pass
            other = candidate
        else:
            return "new", candidate
        if _same_content(src, other):
            return "duplicate", candidate
        n += 1


def _transfer(src: str, dst: str, move: bool) -> None:
    """Copia ou move `src` para `dst` preservando conteúdo e data de modificação.

    - Mover no mesmo volume: renomeação atômica (instantânea, nada é alterado).
    - Demais casos: copia para um arquivo temporário, confere o tamanho, renomeia
      para o nome final e só então (se for mover) apaga a origem.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if move:
        try:
            os.rename(src, dst)  # falha se for outro volume/unidade
            return
        except OSError:
            pass

    tmp = dst + TEMP_SUFFIX
    try:
        shutil.copy2(src, tmp)  # copy2 preserva a data de modificação
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError("o tamanho do arquivo copiado difere do original")
        os.replace(tmp, dst)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        finally:
            raise
    if move:
        os.remove(src)


def run_organizer(
    opts: Options,
    log: LogFn,
    progress: ProgressFn,
    cancel: threading.Event,
) -> Stats:
    """Executa a organização. Não depende da interface gráfica."""
    stats = Stats()
    prefix = "[SIMULAÇÃO] " if opts.dry_run else ""

    log("Procurando arquivos...")
    images, stats.ignored = find_images(opts.origin, opts.dest, opts.recursive)
    stats.total = len(images)
    log(
        f"{stats.total} imagem(ns) encontrada(s); "
        f"{stats.ignored} arquivo(s) que não são imagens foram ignorados."
    )
    progress(0, stats.total)

    planned: dict[str, str] = {}

    for index, src in enumerate(images, start=1):
        if cancel.is_set():
            stats.cancelled = True
            break

        rel_src = os.path.relpath(src, opts.origin)
        try:
            dt, from_exif = get_photo_datetime(src)
            if not from_exif:
                stats.used_mtime += 1

            folder = os.path.join(
                opts.dest, f"{dt:%Y}", f"{dt:%m}", f"{dt:%d}", f"{dt:%H}"
            )
            status, dst = resolve_destination(src, folder, planned)

            if status == "in_place":
                stats.in_place += 1
                log(f"{prefix}Já está no lugar certo: {rel_src}")

            elif status == "duplicate":
                stats.duplicates += 1
                log(
                    f"{prefix}Duplicada (idêntica já existe no destino), ignorada: "
                    f"{rel_src}"
                )

            else:
                if opts.dry_run:
                    planned[os.path.normcase(dst)] = src
                else:
                    _transfer(src, dst, opts.move)
                stats.done += 1

                if opts.dry_run:
                    verb = "Seria movida" if opts.move else "Seria copiada"
                else:
                    verb = "Movida" if opts.move else "Copiada"
                note = ""
                if os.path.basename(dst) != os.path.basename(src):
                    note = " (renomeada para evitar conflito de nome)"
                if not from_exif:
                    note += " [sem data EXIF: usada a data de modificação]"
                log(
                    f"{prefix}{verb}: {rel_src}  →  "
                    f"{os.path.relpath(dst, opts.dest)}{note}"
                )

        except Exception as exc:  # um arquivo com problema não derruba o resto
            stats.errors += 1
            log(f"ERRO em {rel_src}: {exc}")

        progress(index, stats.total)

    return stats


def format_summary(stats: Stats, opts: Options) -> str:
    if opts.dry_run:
        done_label = "Seriam movidas" if opts.move else "Seriam copiadas"
    else:
        done_label = "Movidas" if opts.move else "Copiadas"
    title = "Operação cancelada pelo usuário." if stats.cancelled else "Concluído."
    if opts.dry_run:
        title += " (simulação: nenhum arquivo foi alterado)"
    lines = [
        title,
        f"  Imagens encontradas: {stats.total}",
        f"  {done_label}: {stats.done}",
        f"  Duplicadas (ignoradas): {stats.duplicates}",
        f"  Já no lugar certo: {stats.in_place}",
        f"  Sem data EXIF válida (usada a data de modificação): {stats.used_mtime}",
        f"  Arquivos que não são imagens (ignorados): {stats.ignored}",
        f"  Erros: {stats.errors}",
    ]
    return "\n".join(lines)


# =============================================================================
# 2. INTERFACE GRÁFICA (Tkinter)
# =============================================================================

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, scrolledtext, ttk  # noqa: E402

PREFS_PATH = Path.home() / ".organizador_fotos.json"
APP_TITLE = "Organizador de Fotos por EXIF"


def load_prefs() -> dict:
    try:
        return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs(data: dict) -> None:
    try:
        PREFS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # não é crítico


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(APP_TITLE)
        root.minsize(760, 560)

        prefs = load_prefs()
        self.origin_var = tk.StringVar(value=prefs.get("origin", ""))
        self.dest_var = tk.StringVar(value=prefs.get("dest", ""))
        self.mode_var = tk.StringVar(value=prefs.get("mode", "copy"))
        self.recursive_var = tk.BooleanVar(value=prefs.get("recursive", True))
        self.dry_run_var = tk.BooleanVar(value=False)

        self.queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.closing = False

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(6, weight=1)

        ttk.Label(main, text="Pasta de origem:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(main, textvariable=self.origin_var).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        self.btn_origin = ttk.Button(
            main, text="Procurar...", command=lambda: self._browse(self.origin_var,
                                                                    "Selecione a pasta de origem")
        )
        self.btn_origin.grid(row=0, column=2)

        ttk.Label(main, text="Pasta de destino:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(main, textvariable=self.dest_var).grid(
            row=1, column=1, sticky="ew", padx=6
        )
        self.btn_dest = ttk.Button(
            main, text="Procurar...", command=lambda: self._browse(self.dest_var,
                                                                    "Selecione a pasta de destino")
        )
        self.btn_dest.grid(row=1, column=2)

        mode_box = ttk.LabelFrame(main, text="O que fazer com os arquivos?", padding=8)
        mode_box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.rb_copy = ttk.Radiobutton(
            mode_box, text="Copiar (mantém os originais na pasta de origem)",
            variable=self.mode_var, value="copy",
        )
        self.rb_move = ttk.Radiobutton(
            mode_box, text="Mover (retira os arquivos da pasta de origem)",
            variable=self.mode_var, value="move",
        )
        self.rb_copy.pack(anchor="w")
        self.rb_move.pack(anchor="w")

        self.cb_recursive = ttk.Checkbutton(
            main, text="Incluir subpastas da pasta de origem", variable=self.recursive_var
        )
        self.cb_recursive.grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        self.cb_dry = ttk.Checkbutton(
            main, text="Simular (mostra o que seria feito, sem alterar nenhum arquivo)",
            variable=self.dry_run_var,
        )
        self.cb_dry.grid(row=4, column=0, columnspan=3, sticky="w", pady=2)

        buttons = ttk.Frame(main)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.btn_start = ttk.Button(buttons, text="Iniciar", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(
            buttons, text="Cancelar", command=self._cancel, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(buttons, textvariable=self.status_var).pack(side="left", padx=12)

        self.log_text = scrolledtext.ScrolledText(
            main, height=14, wrap="none", state="disabled", font=("Consolas", 9)
        )
        self.log_text.grid(row=6, column=0, columnspan=3, sticky="nsew")

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        self._inputs = [
            self.btn_origin, self.btn_dest, self.rb_copy, self.rb_move,
            self.cb_recursive, self.cb_dry, self.btn_start,
        ]

    def _browse(self, var: tk.StringVar, title: str) -> None:
        chosen = filedialog.askdirectory(
            title=title, initialdir=var.get() or None, mustexist=True
        )
        if chosen:
            var.set(os.path.normpath(chosen))

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.running = running
        for widget in self._inputs:
            widget.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------ ações ----
    def _start(self) -> None:
        origin = self.origin_var.get().strip()
        dest = self.dest_var.get().strip()

        if not origin or not dest:
            messagebox.showwarning(APP_TITLE, "Informe a pasta de origem e a pasta de destino.")
            return
        origin, dest = os.path.normpath(origin), os.path.normpath(dest)
        if not os.path.isdir(origin):
            messagebox.showerror(APP_TITLE, "A pasta de origem não existe.")
            return
        if not os.path.isdir(dest):
            messagebox.showerror(APP_TITLE, "A pasta de destino não existe.")
            return
        if _norm(origin) == _norm(dest):
            messagebox.showerror(
                APP_TITLE, "A pasta de origem e a pasta de destino não podem ser a mesma."
            )
            return

        opts = Options(
            origin=origin,
            dest=dest,
            move=self.mode_var.get() == "move",
            recursive=self.recursive_var.get(),
            dry_run=self.dry_run_var.get(),
        )

        if opts.move and not opts.dry_run:
            if not messagebox.askyesno(
                APP_TITLE,
                "Os arquivos serão MOVIDOS (retirados) da pasta de origem.\n\n"
                "Deseja continuar?",
            ):
                return

        save_prefs({
            "origin": origin, "dest": dest, "mode": self.mode_var.get(),
            "recursive": opts.recursive,
        })

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.configure(value=0, maximum=1)
        self.status_var.set("Trabalhando...")
        self.cancel_event.clear()
        self._set_running(True)

        self.worker = threading.Thread(target=self._work, args=(opts,), daemon=True)
        self.worker.start()
        self.root.after(100, self._poll)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelando... (aguarde o arquivo atual terminar)")
        self.btn_cancel.configure(state="disabled")

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                APP_TITLE,
                "Há uma operação em andamento.\n\nDeseja cancelar e sair?",
            ):
                return
            self.closing = True
            self._cancel()
        else:
            self.root.destroy()

    # --------------------------------------------- thread de trabalho ----
    def _work(self, opts: Options) -> None:
        # Esta função roda fora da thread principal: só se comunica via fila.
        try:
            stats = run_organizer(
                opts,
                log=lambda m: self.queue.put(("log", m)),
                progress=lambda d, t: self.queue.put(("progress", d, t)),
                cancel=self.cancel_event,
            )
            self.queue.put(("done", stats, opts))
        except Exception as exc:
            self.queue.put(("fatal", str(exc)))

    def _poll(self) -> None:
        finished = False
        try:
            for _ in range(300):  # limite por ciclo para não travar a janela
                kind, *data = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(data[0])
                elif kind == "progress":
                    done, total = data
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(f"Processando {done} de {total}...")
                elif kind == "done":
                    stats, opts = data
                    summary = format_summary(stats, opts)
                    self._append_log("\n" + summary)
                    self.status_var.set("Cancelado." if stats.cancelled else "Concluído.")
                    finished = True
                elif kind == "fatal":
                    self._append_log(f"\nERRO: {data[0]}")
                    messagebox.showerror(APP_TITLE, f"Ocorreu um erro inesperado:\n\n{data[0]}")
                    self.status_var.set("Erro.")
                    finished = True
        except queue.Empty:
            pass

        if finished:
            self._set_running(False)
            if self.closing:
                self.root.destroy()
            return
        self.root.after(100, self._poll)


def main() -> None:
    try:  # texto nítido em telas de alta resolução no Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()