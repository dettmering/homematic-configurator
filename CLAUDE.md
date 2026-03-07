# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Web-based configurator for Homematic thermostat weekly schedules. Communicates with Homegear via XML-RPC to read/write thermostat week programs (time slots with temperatures per day).

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Run dev server
uvicorn backend.app:app --host 0.0.0.0 --port 8080

# Docker
docker build -t homematic-configurator .
docker run -p 8080:8080 homematic-configurator
```

## CLI Tool

`homematic_program.py` is a standalone CLI for setting schedules via JSON file:
```bash
python3 homematic_program.py --rpc http://127.0.0.1:2001/ --peer-id 1234 --file schedule.json --apply
```
Omit `--apply` for dry-run.

## Architecture

- **backend/app.py** — FastAPI app. All API routes and Homegear XML-RPC communication. Serves the frontend as static files. No database; config loaded from `config.yaml` at project root.
- **frontend/** — Vanilla JS/HTML/CSS single-page app (no build step). Loaded via FastAPI static file serving. Static assets referenced as `/static/...`.
- **homematic_program.py** — Standalone CLI tool (no web dependency). Shares concepts with the backend but is independent code.
- **config.yaml** — Runtime config: `rpc_url` (Homegear XML-RPC endpoint), `temp_scale`, `max_slots`, `channel`.

## Key Domain Concepts

- Thermostats have weekly schedules: 7 days, up to 13 time slots per day
- Each slot has an end time (minutes since midnight) and a temperature
- Last slot of each day must end at 24:00 (1440 minutes)
- Homegear paramset keys use either English (`ENDTIME_MONDAY_1`) or German (`ENDTIME_MONTAG_1`) — code auto-detects which variant the device uses
- Temperatures are in Celsius (Homegear returns values in Celsius directly)
- The UI is in German

## API Endpoints

- `GET /api/config` — returns runtime config
- `GET /api/devices` — lists thermostats (filters by known types + ENDTIME_ key detection)
- `GET /api/devices/{peer_id}/schedule` — reads week program from device
- `PUT /api/devices/{peer_id}/schedule` — writes week program to device
