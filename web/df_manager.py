import asyncio
import os
import re
import shutil
import json
from pathlib import Path
from typing import Optional, Callable, Awaitable

DF_DIR = Path(os.environ.get("DF_DIR", "/opt/dwarf-fortress"))


def _saves_dir() -> Path:
    try:
        import settings_manager
        return Path(settings_manager.get_settings()["save_destination_path"])
    except Exception:
        return Path(os.environ.get("SAVES_DIR", "/saves"))

# Stock preset names that match each UI world size (from DF 0.47.05 world_gen.txt)
STOCK_PRESET = {
    "pocket": "POCKET REGION",
    "small":  "SMALLER REGION",
    "medium": "SMALL REGION",
    "large":  "MEDIUM REGION",
    "huge":   "LARGE REGION",
}

WORLD_DIM = {
    "pocket": 17, "small": 33, "medium": 65, "large": 129, "huge": 257,
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
    lines = stock_path.read_text(encoding="cp437", errors="replace").splitlines(keepends=True)

    # Find the TITLE line for this preset
    title_idx = next(
        (i for i, l in enumerate(lines) if l.strip() == f"[TITLE:{preset_title}]"),
        None
    )
    if title_idx is None:
        raise ValueError(f"Preset '{preset_title}' not found in stock world_gen.txt")

    # Walk back to find the [WORLD_GEN] that owns this title
    start = next(
        (i for i in range(title_idx, -1, -1) if lines[i].strip() == "[WORLD_GEN]"),
        None
    )
    if start is None:
        raise ValueError(f"No [WORLD_GEN] header found for preset '{preset_title}'")

    # Find the next [WORLD_GEN] to mark the end of this block
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == "[WORLD_GEN]"),
        len(lines)
    )

    return lines[start:end]


def write_worldgen_params(config: dict) -> str:
    """
    Build a world_gen.txt by reading the matching stock preset and patching
    only the params the user controls. Returns the title token used.
    """
    title = config.get("title", "Generated World").replace(" ", "_")
    size_key = config.get("world_size", "medium")
    stock_name = STOCK_PRESET.get(size_key, "SMALL REGION")
    max_civs = MAX_CIVS.get(size_key, 16)

    dim = WORLD_DIM.get(size_key, 65)

    overrides = {
        "TITLE":                  title,
        "DIM":                    f"{dim}:{dim}",
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


async def _move_generated_world(region_id: int) -> Optional[Path]:
    """Move newly generated region from DF save dir to configured saves location. Returns dest path."""
    save_src = DF_DIR / "data" / "save"
    if not save_src.exists():
        return None

    saves = _saves_dir()
    saves.mkdir(parents=True, exist_ok=True)

    # Find the next unused regionN slot so we never overwrite existing worlds
    existing = {e.name for e in saves.iterdir() if e.is_dir()}
    dest_n = 1
    while f"region{dest_n}" in existing:
        dest_n += 1
    dest = saves / f"region{dest_n}"

    # DF 0.47.05 saves to data/save/region<N>/ or data/save/current/
    candidates = [e for e in save_src.iterdir()
                  if e.is_dir() and e.name.startswith("region")]
    if candidates:
        src = candidates[0]
    elif (save_src / "current").exists():
        src = save_src / "current"
    else:
        await _broadcast(json.dumps({"type": "log", "line": "> Warning: no save found in data/save/"}))
        return None

    shutil.move(str(src), str(dest))
    await _broadcast(json.dumps({"type": "log", "line": f"> World saved to {dest}"}))
    return dest


async def _move_legend_files(region_id: int, save_dest: Path):
    """
    Move region<N>-* export files from DF_DIR into save_dest.
    DF auto-exports these during -gen (world_history.txt, world_sites_and_pops.txt,
    world map bmps, world_gen_param.txt).
    """
    moved = 0
    for f in list(DF_DIR.glob(f"region{region_id}-*")) + list(DF_DIR.glob(f"region{region_id}_*")):
        shutil.move(str(f), str(save_dest / f.name))
        await _broadcast(json.dumps({"type": "log", "line": f"> Legends file: {f.name}"}))
        moved += 1
    if moved == 0:
        await _broadcast(json.dumps({"type": "log", "line": "> No legends files found in DF directory."}))
    else:
        await _broadcast(json.dumps({"type": "log", "line": f"> {moved} legends file(s) saved."}))


def _parse_history(text: str):
    """Parse world_history.txt into world title, subtitle, civs list, flat rulers list."""
    lines = text.splitlines()
    world_title    = lines[0].strip() if lines else ""
    world_subtitle = lines[1].strip() if len(lines) > 1 else ""

    civs: list = []
    all_rulers: list = []
    current_civ     = None
    current_section = None
    current_leader  = None

    for raw in lines[2:]:
        if not raw:
            continue
        indent   = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if indent == 0:
            current_leader  = None
            current_section = None
            if ',' in raw:
                civ_name, race = raw.split(',', 1)
                current_civ = {
                    "name":    civ_name.strip(),
                    "race":    race.strip(),
                    "deities": [],
                    "leaders": [],
                }
                civs.append(current_civ)

        elif indent == 1:
            current_leader = None
            if stripped == "Worship List":
                current_section = "worship"
            elif stripped.endswith(" List"):
                role = stripped[:-5].strip().lower()
                current_section = f"{role}_leaders"

        elif indent == 2 and current_civ is not None:
            if current_section == "worship":
                m = re.match(r'(.+?),\s*(deity|force):\s*(.*)', stripped)
                if m:
                    current_civ["deities"].append({
                        "name":    m.group(1).strip(),
                        "type":    m.group(2),
                        "domains": m.group(3).strip(),
                    })
            elif current_section and current_section.endswith("_leaders") and stripped.startswith("[*]"):
                body = stripped[3:].strip()
                paren     = body.find('(')
                paren_end = body.find(')', paren) if paren != -1 else -1
                name      = body[:paren].strip() if paren != -1 else body
                inner     = body[paren+1:paren_end] if (paren != -1 and paren_end != -1) else ""
                after     = body[paren_end+1:].strip().lstrip(',').strip() if paren_end != -1 else ""

                birth = re.search(r'b\.(\S+)',               inner)
                death = re.search(r'd\.\s*(\d+)',            inner)
                reign = re.search(r'Reign Began:\s*(-?\d+)', inner)

                succession = marital = ""
                for part in re.split(r',\s*', after):
                    p = part.lstrip('* ').strip()
                    if p in ("Original Line", "New Line") or p.startswith("Inherited"):
                        succession = p
                    elif p in ("Never Married", "Divorced") or p.startswith("Married"):
                        marital = p

                role = current_section.replace("_leaders", "")
                current_leader = {
                    "role":        role,
                    "name":        name,
                    "birth":       birth.group(1) if birth else "?",
                    "death":       death.group(1) if death else "",
                    "reign_began": int(reign.group(1)) if reign else None,
                    "succession":  succession,
                    "marital":     marital,
                    "children":    0,
                    "deity":       "",
                    "devotion":    "",
                    "civ":         current_civ["name"],
                }
                current_civ["leaders"].append(current_leader)
                all_rulers.append(current_leader)

        elif indent >= 6 and current_leader is not None:
            if stripped.startswith("No Children"):
                current_leader["children"] = 0
            elif "Children" in stripped:
                m = re.match(r'(\d+)\s+Child', stripped)
                if m:
                    current_leader["children"] = int(m.group(1))
            else:
                m = re.match(r'Worshipp?e?d?\s+(.+?)\s+\((\d+)%\)', stripped)
                if m:
                    current_leader["deity"]    = m.group(1)
                    current_leader["devotion"] = m.group(2) + "%"

    return world_title, world_subtitle, civs, all_rulers


def _parse_sites(text: str):
    """Parse world_sites_and_pops.txt into populations, total pop, and sites list."""
    populations: list = []
    total_pop   = 0
    sites: list = []
    current_site = None
    in_pop   = False
    in_sites = False

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped == "Civilized World Population":
            in_pop = True; in_sites = False; continue
        if stripped == "Sites":
            in_pop = False; in_sites = True; continue

        if in_pop:
            if stripped.startswith("Total:"):
                try:
                    total_pop = int(stripped.split(":", 1)[1].strip().replace(",", ""))
                except ValueError:
                    pass
            elif raw[0] in (' ', '\t'):
                parts = stripped.split(None, 1)
                if len(parts) == 2:
                    try:
                        populations.append({"race": parts[1], "count": int(parts[0].replace(",", ""))})
                    except ValueError:
                        pass

        if in_sites:
            if raw[0] not in (' ', '\t') and ':' in raw:
                colon = raw.index(':')
                rest  = raw[colon+1:].strip()
                q1 = rest.find('"')
                q2 = rest.find('"', q1+1) if q1 != -1 else -1
                english   = rest[q1+1:q2] if (q1 != -1 and q2 != -1) else ""
                native    = rest.split(',')[0].strip()
                site_type = rest.rsplit(',', 1)[-1].strip().strip('"')
                current_site = {
                    "id":        raw[:colon].strip(),
                    "name":      english if english else native,
                    "native":    native,
                    "type":      site_type,
                    "pops":      [],
                    "total_pop": 0,
                }
                sites.append(current_site)
            elif raw[0] in (' ', '\t') and current_site is not None:
                parts = stripped.split(None, 1)
                if len(parts) == 2:
                    try:
                        count = int(parts[0].replace(",", ""))
                        current_site["pops"].append({"creature": parts[1], "count": count})
                        current_site["total_pop"] += count
                    except ValueError:
                        pass

    return populations, total_pop, sites


def _parse_params(text: str):
    """Parse world_gen_param.txt into list of {key, value} pairs."""
    params = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']') and ':' in line:
            content = line[1:-1]
            key, _, value = content.partition(':')
            if key != "WORLD_GEN":
                params.append({"key": key, "value": value})
    return params


def parse_legends(world_name: str) -> dict:
    """Parse all available legend data for a saved world."""
    world_dir = _saves_dir() / world_name
    if not world_dir.exists():
        return {"error": f"World '{world_name}' not found in saves."}

    history_files = list(world_dir.glob("*world_history*.txt"))
    sites_files   = list(world_dir.glob("*world_sites*.txt"))
    param_files   = list(world_dir.glob("*world_gen_param*.txt"))

    if not history_files and not sites_files:
        return {"error": "No legends files found. Generate a world first."}

    world_title = world_name
    world_subtitle = ""
    civs: list = []
    all_rulers: list = []

    if history_files:
        try:
            text = history_files[0].read_text(encoding="cp437", errors="replace")
            world_title, world_subtitle, civs, all_rulers = _parse_history(text)
        except OSError:
            pass

    populations: list = []
    total_pop = 0
    sites: list = []

    if sites_files:
        try:
            text = sites_files[0].read_text(encoding="cp437", errors="replace")
            populations, total_pop, sites = _parse_sites(text)
        except OSError:
            pass

    world_params: list = []
    if param_files:
        try:
            text = param_files[0].read_text(encoding="cp437", errors="replace")
            world_params = _parse_params(text)
        except OSError:
            pass

    # Build deduplicated deity index
    deities_map: dict = {}
    for civ in civs:
        for d in civ["deities"]:
            key = d["name"]
            if key not in deities_map:
                deities_map[key] = {
                    "name":          d["name"],
                    "type":          d["type"],
                    "domains":       d["domains"],
                    "worshipped_by": [],
                }
            deities_map[key]["worshipped_by"].append(civ["name"])
    deities = sorted(deities_map.values(), key=lambda x: x["name"])

    site_types = sorted({s["type"] for s in sites if s["type"]})
    all_files  = sorted(f.name for f in world_dir.glob("*.txt"))

    return {
        "world_title":    world_title,
        "world_subtitle": world_subtitle,
        "total_pop":      total_pop,
        "populations":    populations,
        "entities":       civs,
        "figures":        all_rulers,
        "sites":          sites,
        "site_types":     site_types,
        "deities":        deities,
        "world_params":   world_params,
        "files":          all_files,
        "summary": {
            "civs":    len(civs),
            "rulers":  len(all_rulers),
            "sites":   len(sites),
            "deities": len(deities),
        }
    }


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

        # Drain stdout to avoid pipe blocking — gamelog.txt has the real progress
        async for _ in _active_process.stdout:
            pass

        await _active_process.wait()
        stop_tail.set()
        await tail_task

        rc = _active_process.returncode
        if rc == 0:
            await _broadcast(json.dumps({"type": "progress", "pct": 100}))
            # 1. Move the world save to /saves/region<N>/
            save_dest = await _move_generated_world(region_id)
            if save_dest is None:
                save_dest = _saves_dir() / f"region{region_id}"
                save_dest.mkdir(parents=True, exist_ok=True)
                await _broadcast(json.dumps({"type": "log", "line": "> Warning: world save directory not found."}))
            # 2. Move the auto-exported legends files (region<N>-*) into the save folder
            await _move_legend_files(region_id, save_dest)
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
    saves = _saves_dir()
    if not saves.exists():
        return []
    worlds = []
    for entry in sorted(saves.iterdir()):
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
    world_path = _saves_dir() / name
    if world_path.exists() and world_path.is_dir():
        shutil.rmtree(world_path)
        return True
    return False


def get_df_version() -> str:
    version_file = DF_DIR / "release notes.txt"
    if version_file.exists():
        first_line = version_file.read_text(encoding="cp437", errors="replace").splitlines()[0]
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
