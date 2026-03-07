# Homematic Thermostat Schedule Configurator

A web-based tool for configuring weekly heating schedules on Homematic thermostats via [Homegear](https://homegear.eu/) XML-RPC.

![Screenshot](https://img.shields.io/badge/stack-Python%20%7C%20FastAPI%20%7C%20Vanilla%20JS-blue)

## Features

- Visual timeline editor for weekly thermostat schedules
- Drag-and-drop slot resizing
- Copy schedules between days
- Supports HM-CC-RT-DN, HmIP-eTRV and other Homematic thermostat models
- Auto-detects German/English parameter naming (MONTAG vs MONDAY)
- Standalone CLI tool for batch programming

## Requirements

- [Homegear](https://homegear.eu/) instance with paired Homematic thermostats
- Python 3.12+ or Docker

## Quick Start

### Docker (recommended)

```bash
docker build -t homematic-configurator .
docker run -p 8080:8080 homematic-configurator
```

Open [http://localhost:8080](http://localhost:8080).

### Manual

```bash
pip install -r requirements.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8080
```

## Configuration

Edit `config.yaml`:

```yaml
rpc_url: "http://192.168.0.111:2001/"  # Homegear XML-RPC endpoint
temp_scale: 2.0                         # Temperature scaling factor
max_slots: 13                           # Max time slots per day
channel: 0                              # Device channel for week program
```

## CLI Tool

`homematic_program.py` can set schedules from a JSON file without the web UI:

```bash
# Dry run (preview changes)
python3 homematic_program.py --rpc http://127.0.0.1:2001/ --peer-id 1234 --file schedule.json

# Apply changes
python3 homematic_program.py --rpc http://127.0.0.1:2001/ --peer-id 1234 --file schedule.json --apply
```

Example `schedule.json`:

```json
{
  "days": {
    "MONDAY": [
      {"end": "06:00", "temp": 17.0},
      {"end": "08:30", "temp": 21.0},
      {"end": "17:00", "temp": 17.0},
      {"end": "22:00", "temp": 21.0},
      {"end": "24:00", "temp": 17.0}
    ]
  }
}
```

## License

[MIT](LICENSE)
