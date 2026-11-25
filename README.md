# TinySA & NanoVNA Front Panels for Windows

USB interface programs for the tinySA and the NanoVNA, with graphical instrument panels controlled by the mouse.

Demo videos on YouTube: [tinySA](https://youtu.be/yPIbrWnYdHI) | [NanoVNA](https://youtu.be/Vf9LRCsLcrs)

## Overview

Bench-instrument style Windows front panels for low-cost RF test gear, written in Python (PyQt6 + pyqtgraph).
Each one talks to the instrument over its USB serial shell and includes a simulator, so you can try it without hardware.

| Tool | Instrument | Highlights |
| --- | --- | --- |
| [tinySA Front Panel](tinysa_panel/) | tinySA Ultra / tinySA | Live trace, markers, peak table, max/min hold, averaging, waterfall, generator, screen capture |
| [NanoVNA-F V3 Front Panel](nanovna_panel/) | SYSJOINT NanoVNA-F V3 (NanoVNA shell) | 4 traces, Smith/polar/LogMag/phase/GD/SWR/TDR, markers + BW/Q search, app-side SOLT cal, Touchstone export |

![tinySA panel](docs/tinysa.png)

![NanoVNA panel](docs/nanovna.png)

## Download

Ready-to-run Windows programs (no Python needed), `tinySA_Panel.exe` and `NanoVNA_Panel.exe`,
are attached to each version on the [Releases](../../releases) page. If no release is listed yet,
run from source (below) or build the .exe yourself.

## Run from source on Windows

1. Install Python 3.10 or newer (tick "Add Python to PATH").
2. Open the tool's folder and double-click `run.bat`. The first run installs the dependencies.
   `run_simulator.bat` starts it with the built-in simulator.

```sh
pip install -r tinysa_panel/requirements.txt
python tinysa_panel/tinysa_panel.py --sim
python nanovna_panel/vna_panel.py --sim
```

### Run from source on macOS

Requires Python 3.10 or newer (e.g. `brew install python`). The first run creates `.venv/` in the repo root folder and installs the dependencies of both panels/projects there.

Open `Terminal` application and set up a `brew` package manager from [Homebrew](https://brew.sh)

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Then install Python3
brew install python
```

Open a new `Terminal` for the installation take effect and correct paths. Test if installed python installed. Below is an example output.

```sh
python3 --version
                                                             
Python 3.15.2
```

After cloning this project, run `setup_venv.sh` one time, to setup `virtual enviroment`, where all necessery `pip` packages will be installed.

```sh
export PYTHON=python3  # set PYTHON=python3.12 to pick an interpreter
./setup_venv.sh        # install dependencies only. 
# This will create a .venv/ folder in a project root folder.
# The .venv folder can be savely removed and recreated by running setup_venv/sh again.
```

```sh
./tinysa_panel/run.sh  # or run_simulator.sh; extra args pass through, e.g. --port /dev/cu.usbmodem401
./nanovna_panel/run.sh
```

`build_app.sh` in either folder builds an unsigned MacOSX `tinySA_Panel.app` or `` in that folder's `dist/`. On first launch, right-click it and choose Open to get past Gatekeeper.

## Common controls

- Hard keys open softkey menus on the screen. Click a softkey or use F1-F8 (F1-F7 on the tinySA panel).
- **Knob:** hover over it and use the mouse wheel, or click and drag.
- **Entering values:** type a number on the keypad or the PC keyboard, then pick a unit softkey or press Enter.
- **Markers:** click the trace to drop the active marker.

## Building the .exe yourself

Run `build_exe.bat` in either folder. The program ends up in that folder's `dist\` subfolder.
Pushing a tag such as `v1.0` to GitHub builds both programs automatically and attaches them to a release
(see `.github/workflows/build.yml`).

## License

MIT. See [LICENSE](LICENSE).
