# Dictate

Press **Ctrl+Win**, speak, press it again. Your words are typed into whatever window has focus.

- Runs entirely on your CPU. No account, no cloud, no audio leaves your machine.
- A tray icon shows the state: grey idle, red listening, amber transcribing.
- Click the tray icon to start/stop, and open **Show history** to copy back earlier dictations (kept in memory only, never written to disk).
- Safety nets: it buzzes after 30 s of silence and discards the recording after 90 s, so a forgotten recording cannot run on.
- Windows 10/11, Python 3.10+.

## Install

1. Install [Python 3.10 or newer](https://www.python.org/downloads/) (tick **Add python.exe to PATH**).
2. Download this repository (**Code → Download ZIP**, then unzip) and run in PowerShell inside the folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   Add `-Startup` to launch it at login. The script creates a private `.venv`, installs the dependencies, downloads the speech model once (about 490 MB) and adds a **Dictate** shortcut to the Start menu.
3. Open **Dictate** from the Start menu and start talking.

To remove it: `.\uninstall.ps1` (add `-DeleteModel` to delete the downloaded model), then delete the folder.

## Models

| Name | What it is | Notes |
| --- | --- | --- |
| `parakeet` (default) | NVIDIA Parakeet TDT 0.6B v3, int8, run by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Downloaded automatically to `%LOCALAPPDATA%\dictate\models`. 25 European languages. |
| `phonon` | [Fermion](https://pypi.org/project/fermion-research/) Phonon-2 | Optional: `.venv\Scripts\pip install fermion-research`. Used automatically when installed. Check its own licence terms. |

Choose with `--model parakeet` or the `DICTATE_MODEL` environment variable.

## Use from the command line

```powershell
.\.venv\Scripts\python dictate.py               # run with a console window
.\.venv\Scripts\python dictate.py clip.wav      # transcribe a 16 kHz mono 16-bit WAV file
.\.venv\Scripts\python dictate.py --download    # just fetch the model
```

## Settings

Edit the constants near the top of `dictate.py`: `TOGGLE_MODE` (`False` = hold Ctrl+Win to talk), `THREADS`, and the silence timings.

## Notes

- The keyboard hook lets it see Ctrl+Win; some antivirus tools flag any global keyboard hook, which is expected for this kind of tool.
- It cannot type into windows running as administrator unless Dictate is also run as administrator.

## License

MIT
