import asyncio
import os
import shutil
import json
from pathlib import Path
from typing import Optional, Callable, Awaitable

DF_DIR = Path(os.environ.get("DF_DIR", "/opt/dwarf-fortress"))
SAVES_DIR = Path(os.environ.get("SAVES_DIR", "/saves"))

# Stock preset names that match each UI world size (from DF 0.47.05 world_gen.txt)
STOCK_PRESET = {
    "pocket": "POCKET REGION",
    "small":  "SMALLER REGION",
    "medium": "SMALL REGION",
    "large":  "MEDIUM REGION",
    "huge":   "LARGE REGION",
}

MAX_CIVS = {
    "pocket": 4, "small": 8, "medium": 16, "large": 24, "huge": 40,
}

_active_process: Optional[asyncio.subprocess.Process] = None
_broadcast_callback: Optional[Callable[[str], Awaitable[None]]] = None


def set_broadcast_callback(cb: Callable[[str], Awaitable[None]]):
    global _broadcast_callback
    _broadcast_callback = cb


def is_running() -> bool:
    return _active_process is not None and _active_process.returncode is None


async def _broadcast(msg: str):
    if _broadcast_callback:
        await _broadcast_callback(msg)


def _read_stock_preset(preset_title: str) -> list[str]:
    """Read a named preset block from the backed-up stock world_gen.txt."""
    stock_path = DF_DIR / "data" / "init" / "world_gen_stock.txt"
    text = stock_path.read_text(errors="replace")
    lines = text.splitlines(keepends=True)
    result = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[WORLD_GEN]":
            if in_block:
                break  # start of next block = end of ours
            in_block = True
            result.append(line)
        elif in_block:
            if stripped.startswith(f"[TITLE:{preset_title}]"):
                result.append(line)
            else:
                result.append(line)
    return result


def write_worldgen_params(config: dict) -> str:
    """
    Build a world_gen.txt by reading the matching stock preset and patching
    only the params the user controls. Returns the title token used.
    """
    title = config.get("title", "Generated World").replace(" ", "_")
    size_key = config.get("world_size", "medium")
    stock_name = STOCK_PRESET.get(size_key, "SMALL REGION")
    max_civs = MAX_CIVS.get(size_key, 16)

    overrides = {
        "TITLE":                  title,
        "END_YEAR":               str(int(config.get("history", 250))),
        "MEGABEAST_CAP":          str(int(config.get("megabeast_cap", 18))),
        "SEMIMEGABEAST_CAP":      str(int(config.get("semimegabeast_cap", 37))),
        "TITAN_NUMBER":           str(int(config.get("titan_number", 9))),
        "DEMON_NUMBER":           str(int(config.get("demon_number", 28))),
        "VAMPIRE_NUMBER":         str(int(config.get("vampire_number", 14))),
        "WEREBEAST_NUMBER":       str(int(config.get("werebeast_number", 14))),
        "TOTAL_CIV_NUMBER":       str(min(int(config.get("num_civs", max_civs)), max_civs)),
        "PLAYABLE_CIVILIZATION_REQUIRED": "0",
        # Zero out all minimums to prevent impossible-constraint loops
        "PEAK_NUMBER_MIN":        "0",
        "COMPLETE_OCEAN_EDGE_MIN": "0",
        "VOLCANO_MIN":            "0",
        "RIVER_MINS":             "0:0",
        "GOOD_SQ_COUNTS":        "0:0:0",
        "EVIL_SQ_COUNTS":        "0:0:0",
    }

    # Read stock preset and patch it line by line
    stock_lines = _read_stock_preset(stock_name)

    out_lines = []
    applied = set()
    for line in stock_lines:
        stripped = line.strip()
        matched = False
        for key, val in overrides.items():
            if stripped.startswith(f"[{key}:") or stripped == f"[{key}]":
                out_lines.append(f"\t[{key}:{val}]\n")
                applied.add(key)
                matched = True
                break
        if not matched:
            out_lines.append(line)

    # Append any overrides that weren't in the stock preset
    for key, val in overrides.items():
        if key not in applied:
            out_lines.append(f"\t[{key}:{val}]\n")

    worldgen_path = DF_DIR / "data" / "init" / "world_gen.txt"
    worldgen_path.write_text("".join(out_lines))
    return title


async def _move_generated_world(title: str):
    """Move newly generated region from DF save dir to /saves."""
    save_src = DF_DIR / "data" / "save"
    if not save_src.exists():
        return

    SAVES_DIR.mkdir(parents=True, exist_ok=True)

    for entry in sorted(save_src.iterdir()):
        if entry.is_dir() and entry.name.startswith("region"):
            dest = SAVES_DIR / entry.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(entry), str(dest))
            await _broadcast(json.dumps({
                "type": "log",
                "line": f"> World saved to /saves/{entry.name}"
            }))


async def _tail_gamelog(gamelog_path: Path, stop_event: asyncio.Event):
    """
    Tail DF's gamelog.txt and broadcast new lines as they appear.
    DF writes world generation progress there rather than to stdout.
    """
    pos = 0
    # Skip content that existed before we started
    if gamelog_path.exists():
        pos = gamelog_path.stat().st_size

    progress_keywords = [
        ("placing", 5), ("world gen", 10), ("generating", 15),
        ("civ", 25), ("history", 40), ("export", 80), ("legend", 90),
    ]

    while not stop_event.is_set():
        await asyncio.sleep(0.4)
        if not gamelog_path.exists():
            continue
        try:
            size = gamelog_path.stat().st_size
            if size <= pos:
                continue
            with open(gamelog_path, "r", errors="replace") as f:
                f.seek(pos)
                chunk = f.read(size - pos)
            pos = size
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                await _broadcast(json.dumps({"type": "log", "line": line}))
                ll = line.lower()
                for kw, pct in progress_keywords:
                    if kw in ll:
                        await _broadcast(json.dumps({"type": "progress", "pct": pct}))
                        break
        except OSError:
            pass


async def run_generation(config: dict):
    global _active_process

    title = write_worldgen_params(config)
    region_id = 1

    await _broadcast(json.dumps({"type": "log", "line": "> Worldgen params written."}))
    await _broadcast(json.dumps({"type": "log", "line": f"> Starting DF world generation: {title}"}))
    await _broadcast(json.dumps({"type": "status", "state": "running"}))

    env = os.environ.copy()
    env["DISPLAY"] = ":99"

    df_bin = None
    for candidate in ["df", "libs/Dwarf_Fortress", "Dwarf_Fortress"]:
        p = DF_DIR / candidate
        if p.exists():
            df_bin = p
            break

    if df_bin is None:
        raise FileNotFoundError(f"No DF binary found in {DF_DIR}")

    gamelog = DF_DIR / "gamelog.txt"
    stop_tail = asyncio.Event()

    cmd = [str(df_bin), "-gen", str(region_id), "RANDOM", title]

    try:
        _active_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(DF_DIR),
            env=env,
        )

        # Tail gamelog.txt concurrently for generation progress
        tail_task = asyncio.create_task(_tail_gamelog(gamelog, stop_tail))

        # Stream stdout (startup messages, errors)
        async for raw_line in _active_process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                await _broadcast(json.dumps({"type": "log", "line": line}))

        await _active_process.wait()
        stop_tail.set()
        await tail_task

        rc = _active_process.returncode
        if rc == 0:
            await _broadcast(json.dumps({"type": "progress", "pct": 100}))
            await _move_generated_world(title)
            await _broadcast(json.dumps({"type": "complete", "success": True}))
        else:
            await _broadcast(json.dumps({
                "type": "complete",
                "success": False,
                "error": f"DF exited with code {rc}",
            }))
    except Exception as e:
        stop_tail.set()
        await _broadcast(json.dumps({"type": "complete", "success": False, "error": str(e)}))
    finally:
        _active_process = None


async def cancel_generation():
    global _active_process
    if _active_process and _active_process.returncode is None:
        _active_process.terminate()
        try:
            await asyncio.wait_for(_active_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            _active_process.kill()
        _active_process = None
        await _broadcast(json.dumps({"type": "complete", "success": False, "error": "Cancelled by user"}))
        return True
    return False


def list_worlds() -> list:
    if not SAVES_DIR.exists():
        return []
    worlds = []
    for entry in sorted(SAVES_DIR.iterdir()):
        if entry.is_dir():
            stat = entry.stat()
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            worlds.append({
                "name": entry.name,
                "created": stat.st_mtime,
                "size_bytes": size,
            })
    return worlds


def delete_world(name: str) -> bool:
    world_path = SAVES_DIR / name
    if world_path.exists() and world_path.is_dir():
        shutil.rmtree(world_path)
        return True
    return False


def get_df_version() -> str:
    version_file = DF_DIR / "release notes.txt"
    if version_file.exists():
        first_line = version_file.read_text(errors="replace").splitlines()[0]
        return first_line.strip()
    # Try reading from the binary path name
    return "unknown"


WORLDGEN_PARAM_RANGES = {
    "world_size": {
        "type": "select",
        "options": ["pocket", "small", "medium", "large", "huge"],
        "default": "medium",
        "label": "World Size",
    },
    "history": {
        "type": "select",
        "options": [25, 100, 250, 500, 1050],
        "default": 250,
        "label": "History Length (years)",
    },
    "num_civs": {
        "type": "range",
        "min": 0, "max": 50, "default": 24,
        "label": "Civilizations",
    },
    "megabeast_cap": {
        "type": "range",
        "min": 0, "max": 75, "default": 18,
        "label": "Megabeasts",
    },
    "semimegabeast_cap": {
        "type": "range",
        "min": 0, "max": 150, "default": 37,
        "label": "Semi-Megabeasts",
    },
    "titan_number": {
        "type": "range",
        "min": 0, "max": 50, "default": 9,
        "label": "Titans",
    },
    "demon_number": {
        "type": "range",
        "min": 0, "max": 100, "default": 28,
        "label": "Demons",
    },
    "vampire_number": {
        "type": "range",
        "min": 0, "max": 100, "default": 14,
        "label": "Vampires",
    },
    "werebeast_number": {
        "type": "range",
        "min": 0, "max": 200, "default": 50,
        "label": "Werebeasts",
    },
    "seed": {
        "type": "integer",
        "default": -1,
        "label": "Seed (-1 = random)",
    },
    "title": {
        "type": "text",
        "default": "Generated World",
        "label": "World Name",
    },
}
