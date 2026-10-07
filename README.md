# RIN-Live

Android application for real-time journey tracking and SAQ (Stufe der Angebotsqualität) evaluation according to **RIN 2008** (FGSV guidelines for integrated network design).

Built with **Python 3.12** and **[Flet](https://flet.dev)** (Flutter runtime).

---

## Architecture Overview

- **Core / UI:** Python 3.12 + Flet (`main.py`, `views/`).
- **Domain & Persistence:** SQLite for time-series geospatial points and SAQ calculations (`models.py`).
- **Packaging & Config:** PEP 621 compliant `pyproject.toml`.
- **Target Platform:** Android APK (Foreground Service + Background Location).

---

## Development Setup

### Option 1: Docker Dev Environment (Recommended)

Requires only Docker & Docker Compose. Completely isolates dependencies from the host system with instant hot-reload.

```bash
# Start local web preview with hot-reload on port 8550
docker compose up

# Stop service
docker compose down
```

Open **[http://localhost:8550](http://localhost:8550)** in your browser. Any edits saved in the workspace trigger hot-reload inside the container.

---

### Option 2: Local Python 3.12 Virtual Environment

If running directly on the host machine:

```bash
# 1. Create and activate a Python 3.12 virtual environment
# Windows:
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS:
python3.12 -m venv .venv
source .venv/bin/activate

# 2. Install package in editable mode with dev dependencies
pip install --upgrade pip
pip install -e ".[dev]"

# 3. Start local development server (web preview)
flet run --web --port 8550
```

---

## Building the Android APK

### 1. CI/CD: GitHub Actions (Recommended — Zero Local Toolchain)

A dedicated GitHub Actions workflow ([`.github/workflows/build-apk.yml`](.github/workflows/build-apk.yml)) handles Flutter SDK, Android SDK, and Java dependencies in a disposable runner.

1. Push your changes to the `flet` branch.
2. In GitHub, navigate to **Actions** ➔ **Build Android APK**.
3. Click **Run workflow**, select branch `flet`, and confirm.
4. Download the compiled `RIN-Live-release-apk` zip under **Artifacts** once the job finishes (~4–5 min).

---

### 2. Local APK Build (Requires Android & Flutter Toolchains)

If building locally on a machine with Flutter SDK, Android SDK (API 34+), and JDK 17 installed:

```bash
flet build apk --project rin --product "RIN-Live" --permissions location
```

Output binary:
```
build/apk/app-release.apk
```

---

## Android Permissions & Deployment

1. **Sideload**: Transfer and install `app-release.apk` on device.
2. **Background Location (Mandatory)**: 
   Go to **Settings** ➔ **Apps** ➔ **RIN-Live** ➔ **Permissions** ➔ **Location** ➔ Select **"Allow all the time"** (*Immer zulassen*).
   *Required for continuous GPS logging via Foreground Service when screen is locked or app is minimized.*
