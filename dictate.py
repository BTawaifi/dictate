"""Press Ctrl+Win, speak, press it again: transcribes locally and pastes into the focused window.

A tray icon shows the state: grey idle, red listening, amber transcribing.
Everything runs on your CPU; no audio leaves the machine. The speech model is
downloaded once, on first run, and then used offline.

`python dictate.py recording.wav` transcribes a 16 kHz mono 16-bit file instead.
"""
import ctypes
import importlib.util
import os
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import winsound
from ctypes import wintypes

import keyboard
import numpy as np
import pystray
import sherpa_onnx
import sounddevice as sd
from PIL import Image, ImageDraw

# Which model to run (pick with `--model <name>` or the DICTATE_MODEL environment variable):
#
#   parakeet  NVIDIA Parakeet TDT 0.6B v3 (int8, 25 European languages), run by
#             sherpa-onnx. About 490 MB, downloaded automatically on first run to
#             %LOCALAPPDATA%\dictate\models. This is the default.
#   phonon    Fermion Phonon-2, a ~2-bit quantised build of the same family with its own
#             CPU engine. Optional: install it with `pip install fermion-research`.
#             It is used by default when that package is installed.
PARAKEET_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
PARAKEET_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                + PARAKEET_NAME + ".tar.bz2")
PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
MODELS = ("parakeet", "phonon")
DEFAULT_MODEL = "phonon" if importlib.util.find_spec("fermion") else "parakeet"
MODELS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "dictate", "models")

# Re-measured 2026-09-30 on i5-1135G7: RTF 0.207 / 0.130 / 0.111 / 0.112 / 0.141 / 0.181
# for 1/2/3/4/6/8 threads. 3 and 4 are a 0.6% difference -- noise -- so 4 stays.
THREADS = 4
RATE = 16000
MIN_SECONDS = 0.3  # ignore accidental taps
# True: press Ctrl+Win once to start, again to stop. False: hold to talk, release to stop.
TOGGLE_MODE = True
# Toggle mode only (a held key can't be forgotten): while recording, buzz every
# SILENCE_BUZZ_EVERY seconds without sound, and discard the recording (no transcript)
# after SILENCE_CANCEL_AFTER. Sound = chunk RMS above SILENCE_RMS (~ -40 dBFS).
SILENCE_BUZZ_EVERY = 30
SILENCE_CANCEL_AFTER = 90
SILENCE_RMS = 0.01
HISTORY_MAX = 200  # kept in memory only, never written to disk (see SHOW_TEXT)
SHOW_TEXT = True  # print transcripts (console only; off when logging to file)



def download_parakeet(target):
    """Fetch and unpack the model next to the others, atomically, with a progress line."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    print(f"Downloading the speech model (~490 MB, one time) to {target}", flush=True)
    archive = target + ".download"
    try:
        with urllib.request.urlopen(PARAKEET_URL) as resp, open(archive, "wb") as out:
            total, done, shown = int(resp.headers.get("Content-Length", 0)), 0, -1
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total and done * 100 // total != shown:
                    shown = done * 100 // total
                    print(f"\r  {shown}%  ({done >> 20} / {total >> 20} MB)", end="", flush=True)
        print("\nUnpacking...", flush=True)
        with tempfile.TemporaryDirectory(dir=MODELS_DIR) as stage:
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(stage, filter="data")
            os.replace(os.path.join(stage, PARAKEET_NAME), target)
    finally:
        if os.path.exists(archive):
            os.remove(archive)


def resolve_model(name=None):
    """Pick the model, downloading parakeet on first use. Fail loudly, never fall back silently."""
    name = name or os.environ.get("DICTATE_MODEL") or DEFAULT_MODEL
    if name not in MODELS:
        raise SystemExit(f"unknown model {name!r}; choose one of: {', '.join(MODELS)}")
    if name == "phonon":
        if not importlib.util.find_spec("fermion"):
            raise SystemExit("model 'phonon' needs the optional package: pip install fermion-research")
        return name, "phonon-2"
    path = os.path.join(MODELS_DIR, PARAKEET_NAME)
    if not all(os.path.exists(os.path.join(path, f)) for f in PARAKEET_FILES):
        if os.path.isdir(path):
            raise SystemExit(f"{path} is incomplete; delete that folder and run again")
        download_parakeet(path)
    return name, path


def load(model_dir):
    if model_dir == "phonon-2":
        import fermion
        return fermion.load_speech(model_dir)
    m = lambda f: os.path.join(model_dir, f)
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=m("encoder.int8.onnx"), decoder=m("decoder.int8.onnx"),
        joiner=m("joiner.int8.onnx"), tokens=m("tokens.txt"),
        num_threads=THREADS, model_type="nemo_transducer",
    )



def transcribe(rec, samples):
    if hasattr(rec, "transcribe_array"):  # Phonon: 16 kHz mono float32 in
        return rec.transcribe_array(samples)[0].strip()
    s = rec.create_stream()
    s.accept_waveform(RATE, samples)
    rec.decode_stream(s)
    return s.result.text.strip()


CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
# Explicit prototypes: without them ctypes returns c_int and truncates the 64-bit
# HANDLE, which faults on GlobalLock.
u32.OpenClipboard.argtypes, u32.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
u32.EmptyClipboard.argtypes, u32.EmptyClipboard.restype = [], wintypes.BOOL
u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
u32.SetClipboardData.restype = wintypes.HANDLE
k32.GlobalAlloc.argtypes, k32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
k32.GlobalLock.argtypes, k32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


def set_clipboard(text):
    # CF_UNICODETEXT is NUL-terminated UTF-16, so it takes no BOM. Adding one
    # would paste a literal U+FEFF, which is how clip.exe got Unicode in the first
    # place -- every dictation used to carry that invisible character.
    for _ in range(50):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.004)  # another app holds it: back off rather than spin
    else:
        raise RuntimeError("clipboard stayed locked")
    try:
        u32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise MemoryError("GlobalAlloc failed")
        ptr = k32.GlobalLock(handle)
        if not ptr:
            raise MemoryError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        u32.CloseClipboard()


# Clicking the tray icon makes the taskbar the active window, so the paste would land
# there. A thread remembers the last window that was neither the shell nor ours, and
# focus_target() hands focus back to it before pasting.
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                 "TopLevelWindowForOverflowXamlIsland"}
u32.GetForegroundWindow.argtypes, u32.GetForegroundWindow.restype = [], wintypes.HWND
u32.SetForegroundWindow.argtypes, u32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
u32.IsWindow.argtypes, u32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
u32.IsIconic.argtypes, u32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
u32.ShowWindow.argtypes, u32.ShowWindow.restype = [wintypes.HWND, ctypes.c_int], wintypes.BOOL
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
last_target = None


def shell_or_ours(hwnd):
    cls, pid = ctypes.create_unicode_buffer(64), wintypes.DWORD()
    u32.GetClassNameW(hwnd, cls, 64)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return cls.value in SHELL_CLASSES or pid.value == os.getpid()


def track_target():
    global last_target
    while True:
        fg = u32.GetForegroundWindow()
        if fg and not shell_or_ours(fg):
            last_target = fg
        time.sleep(0.1)


def focus_target():
    """Return True once a real app window has focus (restoring the last one if needed)."""
    fg = u32.GetForegroundWindow()
    if fg and not shell_or_ours(fg):
        return True
    target = last_target
    if not target or not u32.IsWindow(target):
        return False
    if u32.IsIconic(target):
        u32.ShowWindow(target, 9)  # SW_RESTORE
    for _ in range(2):
        u32.SetForegroundWindow(target)
        for _ in range(10):
            time.sleep(0.02)
            if u32.GetForegroundWindow() == target:
                time.sleep(0.05)  # let the app take focus before Ctrl+V
                return True
        # Windows may refuse; a synthetic Alt tap (sent while the shell is still
        # active, so no app menu reacts) lifts the restriction for one retry.
        u32.keybd_event(0x12, 0, 0, 0)
        u32.keybd_event(0x12, 0, 2, 0)
    return False


def paste(text):
    # Direct Win32 clipboard: 0.17 ms against clip.exe's 29-34 ms per spawn.
    set_clipboard(text)
    keyboard.send("ctrl+v")


history = []  # (time string, text), newest last; appended from the transcribe thread
history_lock = threading.Lock()
history_open = threading.Event()  # a history window exists
history_raise = threading.Event()  # ask that window to come to the front


def add_history(text):
    with history_lock:
        history.append((time.strftime("%H:%M:%S"), text))
        del history[:-HISTORY_MAX]


def show_history():
    """Open the history window, or raise it if it is already open."""
    if history_open.is_set():
        history_raise.set()
        return
    history_open.set()
    threading.Thread(target=_history_window, daemon=True).start()


def _history_window():
    # Tk is not thread-safe: everything below runs on this one thread, and the
    # other threads only talk to it through history / history_raise, polled by after().
    import tkinter as tk
    from tkinter import ttk

    try:
        root = tk.Tk()
        root.title("Dictate history")
        root.geometry("640x420")
        shown = []  # snapshot the list is currently displaying

        top = ttk.Frame(root)
        top.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        lst = tk.Listbox(top, exportselection=False, activestyle="none")
        sb = ttk.Scrollbar(top, command=lst.yview)
        lst.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        lst.pack(side="left", fill="both", expand=True)
        full = tk.Text(root, height=7, wrap="word", state="disabled")
        full.pack(fill="x", padx=8, pady=8)
        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        status = ttk.Label(bar)

        def say(msg):
            status.config(text=msg)
            root.after(2000, lambda: status.config(text=""))

        def refresh():
            with history_lock:
                snap = list(history)
            if snap != shown:
                keep = lst.curselection()
                shown[:] = snap
                lst.delete(0, "end")
                for t, text in reversed(snap):  # newest first
                    lst.insert("end", f"{t}  {text.replace(chr(10), ' ')}")
                if keep and keep[0] < len(snap):
                    lst.selection_set(keep[0])
                show_selected()
            if history_raise.is_set():
                history_raise.clear()
                root.deiconify()
                root.lift()
                root.focus_force()
            root.after(500, refresh)

        def selected_text():
            sel = lst.curselection()
            return shown[len(shown) - 1 - sel[0]][1] if sel else None

        def show_selected(_=None):
            full.config(state="normal")
            full.delete("1.0", "end")
            full.insert("1.0", selected_text() or "")
            full.config(state="disabled")

        def copy_selected(_=None):
            text = selected_text()
            if text is None:
                say("Select an entry first")
                return
            set_clipboard(text)
            say("Copied")

        def copy_all():
            if not shown:
                say("Nothing to copy")
                return
            set_clipboard("\n".join(f"[{t}] {text}" for t, text in shown))
            say("Copied all")

        def clear():
            with history_lock:
                history.clear()
            refresh_now()
            say("Cleared")

        def refresh_now():
            # refresh() reschedules itself; run its body without a second timer chain
            with history_lock:
                snap = list(history)
            shown[:] = snap
            lst.delete(0, "end")
            show_selected()

        lst.bind("<<ListboxSelect>>", show_selected)
        lst.bind("<Double-Button-1>", copy_selected)
        ttk.Button(bar, text="Copy selected", command=copy_selected).pack(side="left")
        ttk.Button(bar, text="Copy all", command=copy_all).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear history", command=clear).pack(side="left")
        status.pack(side="right")
        refresh()
        root.mainloop()
    finally:
        history_open.clear()
        history_raise.clear()


def mic_icon(color):
    im = Image.new("RGBA", (64, 64))
    d = ImageDraw.Draw(im)
    d.ellipse((2, 2, 62, 62), fill=color)
    d.rounded_rectangle((25, 12, 39, 38), radius=7, fill="white")
    d.arc((18, 22, 46, 46), 0, 180, fill="white", width=4)
    d.line((32, 46, 32, 53), fill="white", width=4)
    d.line((24, 53, 40, 53), fill="white", width=4)
    return im


# Tray icon per state: loading / idle / recording / transcribing.
STATES = {
    "load": (mic_icon("#8a8a90"), "Dictate: loading model..."),
    "idle": (mic_icon("#6e6e73"), "Dictate: " + ("press" if TOGGLE_MODE else "hold") + " Ctrl+Win"),
    "rec": (mic_icon("#e5372c"), "Dictate: listening..."),
    "busy": (mic_icon("#e0a000"), "Dictate: transcribing..."),
}


def main(model=None):
    # One instance only: two hooks would each paste every dictation.
    # k32/u32 are the module-level WinDLL handles above.
    _ = k32.CreateMutexW(None, False, "Local\\dictate-ctrl-win")  # held until exit
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit("dictate is already running")
    quit_ = threading.Event()
    tray = pystray.Icon("dictate", STATES["load"][0], STATES["load"][1],
                        pystray.Menu(
                            # default=True: runs on a left click of the tray icon
                            pystray.MenuItem("Start/stop dictation", lambda: toggle(), default=True),
                            pystray.MenuItem("Show history", lambda: show_history()),
                            pystray.MenuItem("Quit", lambda: quit_.set())))

    def show(state):
        tray.icon, tray.title = STATES[state]

    model_name, model_dir = resolve_model(model)
    print(f"Model: {model_name}", flush=True)

    chunks, recording, busy = [], threading.Event(), threading.Lock()
    # scan code -> name. Keyed by scan code because the name follows Shift state:
    # Shift+1 goes down as "!" and, if Shift is released first, comes up as "1".
    held = {}
    ctrl_win = lambda: (any("ctrl" in k for k in held.values())
                        and any("windows" in k for k in held.values()))

    # Session init is the whole startup cost, and ~22 s of it is one-time-per-process
    # native work: first build 25.3 s, second 3.3 s, third 3.4 s. Build on a thread so
    # the tray and hotkey are live immediately; a dictation that lands first waits here.
    rec, model_ready, load_error = None, threading.Event(), []

    def build_model():
        nonlocal rec
        started = time.perf_counter()
        try:
            rec = load(model_dir)
        except Exception as exc:
            load_error.append(exc)
        finally:
            model_ready.set()
            if not recording.is_set() and not busy.locked():
                show("idle")
        if load_error:
            print(f"[model load failed: {load_error[0]}]", flush=True)
        else:
            print(f"Model {model_name!r} ready in {time.perf_counter()-started:.1f}s", flush=True)

    threading.Thread(target=build_model, daemon=True).start()

    def physically_down(sc, name):
        if "ctrl" in name:
            vks = [0x11]  # VK_CONTROL
        elif "windows" in name:
            vks = [0x5B, 0x5C]  # VK_LWIN, VK_RWIN
        else:
            vks = [u32.MapVirtualKeyW(sc, 3)]  # MAPVK_VSC_TO_VK_EX
        return any(vk and u32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)

    def on_audio(data, *_):
        nonlocal last_sound
        if recording.is_set():
            chunk = data[:, 0].copy()
            chunks.append(chunk)
            now = time.perf_counter()
            if now >= ignore_until and np.sqrt(np.mean(chunk ** 2)) > SILENCE_RMS:
                last_sound = now

    last_sound = deadline_buzz = ignore_until = 0.0  # perf_counter times

    def buzz(freq=180, ms=300):
        nonlocal ignore_until
        # The mic can hear the speaker: don't let our own buzz count as sound.
        ignore_until = time.perf_counter() + ms / 1000 + 0.3
        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()

    def silence_check():
        nonlocal deadline_buzz
        if not ((TOGGLE_MODE or by_click) and recording.is_set()):
            return
        quiet = time.perf_counter() - last_sound
        if quiet >= SILENCE_CANCEL_AFTER:
            recording.clear()
            chunks.clear()
            show("idle")
            print(f"[cancelled: {quiet:.0f}s without sound]", flush=True)
            buzz(120, 600)
        elif quiet >= deadline_buzz:
            deadline_buzz += SILENCE_BUZZ_EVERY
            buzz()

    by_click = False  # recording began from the tray: key releases must not end it

    def start_recording(from_click=False):
        nonlocal last_sound, deadline_buzz, by_click
        by_click = from_click
        last_sound = time.perf_counter()
        deadline_buzz = SILENCE_BUZZ_EVERY
        chunks.clear()
        recording.set()
        show("rec")

    def stop_recording():
        recording.clear()
        threading.Thread(target=finish, daemon=True).start()

    def toggle():  # tray click; runs on pystray's thread
        if recording.is_set():
            stop_recording()
        elif not busy.locked():
            start_recording(from_click=True)

    chord_down = False  # toggle mode: was Ctrl+Win already down at the previous event?

    def on_key(e):
        nonlocal chord_down
        name = (e.name or "").lower()
        if e.event_type == "down":
            # Drop keys whose key-up the hook never saw (Win+L, Ctrl+Alt+Del, UAC).
            for sc in [sc for sc, n in held.items() if sc != e.scan_code and not physically_down(sc, n)]:
                del held[sc]
            held[e.scan_code] = name
            others = [k for k in held.values() if "ctrl" not in k and "windows" not in k]
            chord = ctrl_win()
            # Hold mode drops on any other key; toggle mode only when it is part of the
            # chord (Ctrl+Win+<key>), so typing with Ctrl held mid-dictation is harmless.
            if others:  # Ctrl+Win+<key> is a Windows shortcut: drop the recording
                if recording.is_set() and (not TOGGLE_MODE or chord):
                    recording.clear()
                    show("idle")
            elif TOGGLE_MODE:
                # Fire on the press edge only; key auto-repeat re-sends "down".
                if chord and not chord_down:
                    if recording.is_set():
                        stop_recording()
                    elif not busy.locked():
                        start_recording()
            elif chord and not recording.is_set() and not busy.locked():
                start_recording()
            chord_down = chord
        else:
            held.pop(e.scan_code, None)
            chord_down = ctrl_win()
            if not TOGGLE_MODE and not by_click and recording.is_set() and not chord_down:
                stop_recording()

    def finish():
        with busy:
            try:
                _finish()
            finally:
                show("idle")

    def _finish():
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        if len(audio) < MIN_SECONDS * RATE:
            print(f"[ignored: {len(audio)/RATE:.2f}s audio]", flush=True)
            return
        show("busy")
        model_ready.wait()  # first launch: still building; later calls return at once
        if load_error:
            print(f"[model unavailable: {load_error[0]}]", flush=True)
            return
        t = time.perf_counter()
        text = transcribe(rec, audio)
        shown = text if SHOW_TEXT else f"{len(text)} chars"
        print(f"[{len(audio)/RATE:.1f}s audio, {time.perf_counter()-t:.2f}s] {shown}", flush=True)
        if text:
            add_history(text)
        # Ctrl+V with Win still down is Ctrl+Win+V (sound settings); wait for release.
        deadline = time.time() + 3
        while any("windows" in k for k in held.values()) and time.time() < deadline:
            time.sleep(0.02)
        if text:
            if focus_target():
                paste(text + " ")
            else:  # no window to paste into: leave it on the clipboard instead
                set_clipboard(text + " ")
                print("[could not restore focus; text left on the clipboard]", flush=True)

    threading.Thread(target=track_target, daemon=True).start()
    keyboard.hook(on_key)
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=on_audio):
        verb = "Press Ctrl+Win to start/stop" if TOGGLE_MODE else "Hold Ctrl+Win"
        print(f"Ready. {verb} dictating, Ctrl+C to quit.", flush=True)
        tray.run_detached()
        try:
            while not quit_.wait(0.5):  # short waits keep Ctrl+C responsive
                silence_check()
        finally:
            tray.stop()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Push-to-talk dictation.")
    ap.add_argument("wav", nargs="?", help="transcribe this file instead of running the app")
    ap.add_argument("--download", action="store_true", help="fetch the speech model, then exit")
    ap.add_argument("--model", choices=MODELS,
                    help=f"encoder to use (default: $DICTATE_MODEL, else {DEFAULT_MODEL!r})")
    args = ap.parse_args()

    if args.download:
        print("Model ready:", resolve_model(args.model)[1])
    elif args.wav:
        import wave
        with wave.open(args.wav) as w:
            assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        name, path = resolve_model(args.model)
        print(f"[model: {name}]", file=sys.stderr, flush=True)
        rec = load(path)
        t = time.perf_counter()
        print(transcribe(rec, pcm))
        print(f"{len(pcm)/RATE:.1f}s audio in {time.perf_counter()-t:.2f}s", file=sys.stderr)
    else:
        if sys.stdout is None:  # pythonw (Startup shortcut): no console, log to a file
            log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictate.log"),
                       "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
            SHOW_TEXT = False  # keep dictated text out of the on-disk log
            print(time.strftime("--- %Y-%m-%d %H:%M:%S start"), flush=True)
        main(args.model)
