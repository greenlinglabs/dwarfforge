# Dwarf Forge

A Dockerized web UI for headless Dwarf Fortress world generation using the DFHack fork.

## Requirements

- Ubuntu Server (or any Linux host with Docker)
- Docker Engine + Docker Compose v2

## Setup

```bash
# Install Docker (if not already installed)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Clone and run
git clone <repo-url>
cd dwarf-forge
docker compose up --build
```

Open `http://<server-ip>:8000` in your browser.

## Usage

1. **Configure** world parameters in the left panel (size, history length, creature counts, seed).
2. Click **⚒ FORGE WORLD** to start generation.
3. Watch real-time output stream in the **Generation Log** panel.
4. Switch to the **Worlds** tab to browse, inspect, and delete saved worlds.

## Notes

- World generation can take several minutes depending on map size and history length.
- Only one generation job runs at a time. The **✕ CANCEL** button terminates the active job.
- Worlds are saved to a named Docker volume (`dwarf-worlds`) at `/saves` inside the container.
- DF is run headlessly via Xvfb on display `:99`.

## Ports

| Port | Service |
|------|---------|
| 8000 | Web UI + API |

## Volume

| Volume        | Mount     | Purpose            |
|---------------|-----------|--------------------|
| `dwarf-worlds`| `/saves`  | Generated world saves |

## Rebuild after code changes

```bash
docker compose up --build
```

## Stop

```bash
docker compose down
```
