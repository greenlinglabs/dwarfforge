"""
Prints two lines to stdout:
  1. Bay12Games DF 0.47.05 Linux download URL
  2. DFHack 0.47.05-rX Linux overlay download URL (latest rX for 0.47.05)
"""
import urllib.request, json, sys

DF_URL = "http://www.bay12games.com/dwarves/df_47_05_linux.tar.bz2"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "DwarfForge/1.0"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# Try specific tag first, then fall back to searching pages
DFHACK_URL = None
for tag in ["0.47.05-r9", "0.47.05-r8", "0.47.05-r7", "0.47.05-r6", "0.47.05-r5"]:
    try:
        rel = fetch(f"https://api.github.com/repos/DFHack/dfhack/releases/tags/{tag}")
        matches = [
            a["browser_download_url"] for a in rel.get("assets", [])
            if ("Linux" in a["name"] or "linux" in a["name"])
            and "64" in a["name"]
            and a["name"].endswith(".tar.bz2")
        ]
        if matches:
            DFHACK_URL = matches[0]
            print(f"Found DFHack {tag}: {DFHACK_URL}", file=sys.stderr)
            break
        else:
            print(f"Tag {tag} exists but no Linux64 .tar.bz2 asset. Assets: {[a['name'] for a in rel.get('assets',[])]}", file=sys.stderr)
    except Exception as e:
        print(f"Tag {tag} not found: {e}", file=sys.stderr)

if not DFHACK_URL:
    print("ERROR: Could not find any DFHack 0.47.05-rX Linux release", file=sys.stderr)
    sys.exit(1)

print(f"DF URL     : {DF_URL}", file=sys.stderr)
print(f"DFHack URL : {DFHACK_URL}", file=sys.stderr)

print(DF_URL)
print(DFHACK_URL)
