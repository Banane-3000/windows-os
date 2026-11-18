#!/usr/bin/env python3
"""Windows-OS installer: a small local web server to install Windows-OS and its
games on a NumWorks n0120 with one click, from a Mac or a Windows PC.

Only listens on 127.0.0.1. Never writes anything outside of slot B: before
flashing, it checks that slot B is the inactive slot (i.e. that the
calculator is running its official software from slot A).

Usage: server.py [--port 8767] [--no-browser]

Needs Python 3 and libusb:
  - macOS:   brew install libusb   (dfu-util is used too when present)
  - Windows: python -m pip install libusb-package   (done by the .bat file)
Adding new games needs the Arm GNU toolchain (arm-none-eabi-gcc); the games
already in games/ when the installer was built are pre-linked and need
nothing.
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
DFU_LAUNCH = HERE / "dfu_launch.py"  # NumWorks' dfu.py, with libusb found
LINK_GAMES = HERE / "link_games.py"
FIRMWARE_DFU = HERE / "firmware" / "windows-os.B.dfu"
FIRMWARE_ELF = HERE / "firmware" / "windows-os.B.elf"
PREBUILT_GAMES = HERE / "firmware" / "games.bin"
PREBUILT_LIST = HERE / "firmware" / "games.json"
GAMES_DIR = HERE / "games"
PAGE = HERE / "index.html"

PYTHON = sys.executable
WINDOWS = os.name == "nt"


def find_dfu_util():
    path = shutil.which("dfu-util")
    if path:
        return path
    for candidate in ("/opt/homebrew/bin/dfu-util", "/usr/local/bin/dfu-util"):
        if os.path.exists(candidate):
            return candidate
    return None


DFU_UTIL = find_dfu_util()

USERLAND_B = 0x90410000
SLOT_B_START = 0x90400000
SLOT_B_END = 0x90800000
MAX_UPLOAD = 4 * 1024 * 1024


def apps_area():
    """(start, end) of the external apps area of the built firmware: it
    starts right after the userland, so it moves when the userland grows."""
    sys.path.insert(0, str(HERE))
    import link_games
    sym = link_games.symbols(FIRMWARE_ELF)
    start = sym["_external_apps_flash_start"]
    end = sym["_external_apps_flash_end"]
    # Never write outside of slot B
    assert SLOT_B_START + 0x10000 < start < end <= SLOT_B_END, (hex(start), hex(end))
    return start, end

# ------------------------------------------------------------------ helpers


def run(cmd, timeout=600):
    kwargs = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                       timeout=timeout, **kwargs)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def dfu(*args, timeout=600):
    return run([PYTHON, str(DFU_LAUNCH), *args], timeout=timeout)


def dfu_query(flag):
    code, out = dfu(flag, timeout=20)
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    if code != 0 or not lines or "Error" in out:
        return None
    return lines[-1]


def calculator_status():
    model = dfu_query("--model")
    if model is None:
        return {"connected": False}
    slot = dfu_query("--inactive_slot")
    return {"connected": True, "model": model, "inactiveSlot": slot}


def elf_section(path, wanted):
    """Contents of a section of an ELF32 little-endian file, or None."""
    try:
        data = Path(path).read_bytes()
        if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
            return None
        shoff, = struct.unpack_from("<I", data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)

        def header(i):
            return struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize)

        names = header(shstrndx)
        for i in range(shnum):
            h = header(i)
            start = names[4] + h[0]
            name = data[start:data.index(b"\0", start)].decode("ascii", "replace")
            if name == wanted:
                return data[h[4]:h[4] + h[5]]
    except (OSError, ValueError, struct.error, IndexError):
        return None
    return None


def game_name(path):
    raw = elf_section(path, ".rodata.eadk_app_name")
    if not raw:
        return None
    return raw.split(b"\0")[0].decode("utf-8", "replace") or None


def list_games():
    games = []
    for p in sorted(GAMES_DIR.glob("*.nwa"), key=lambda p: p.name.lower()):
        games.append({"file": p.name, "name": game_name(p) or p.stem,
                      "size": p.stat().st_size})
    return games


def games_fingerprint():
    return sorted((p.name.lower(), hashlib.sha1(p.read_bytes()).hexdigest())
                  for p in GAMES_DIR.glob("*.nwa"))


def prebuilt_matches():
    """True if games.bin was built (by the GitHub workflow) for exactly the
    games that are in games/ now."""
    if not PREBUILT_GAMES.exists() or not PREBUILT_LIST.exists():
        return False
    try:
        listed = json.loads(PREBUILT_LIST.read_text())
        expected = sorted((g["name"].lower(), g["sha1"]) for g in listed["games"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return expected == games_fingerprint()


def have_toolchain():
    sys.path.insert(0, str(HERE))
    try:
        import link_games
        link_games.tool("arm-none-eabi-gcc")
        return True
    except SystemExit:
        return False
    except Exception:
        return False


def safe_game_path(filename):
    name = unquote(filename).replace("\\", "/").split("/")[-1]
    if not name.lower().endswith(".nwa") or name.startswith("."):
        return None
    return GAMES_DIR / name


# ---------------------------------------------------------------- install job


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.steps = []
        self.log = []
        self.result = None  # "ok" | "error" | None

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "steps": list(self.steps),
                    "log": "\n".join(self.log[-200:]), "result": self.result}

    def step(self, label, state="running"):
        with self.lock:
            if self.steps and self.steps[-1]["state"] == "running":
                self.steps[-1]["state"] = "done"
            self.steps.append({"label": label, "state": state})

    def fail(self, message):
        with self.lock:
            if self.steps:
                self.steps[-1]["state"] = "error"
            self.log.append("ERREUR: " + message)
            self.result = "error"
            self.running = False

    def write(self, text):
        with self.lock:
            for line in text.replace("\r", "\n").splitlines():
                # Drop progress-bar noise
                if line.strip() and "%" not in line:
                    self.log.append(line.rstrip())


JOB = Job()


def prepare_games(image):
    """Writes the games image to `image`. Returns an error message or None."""
    games = list(GAMES_DIR.glob("*.nwa"))
    if games and prebuilt_matches():
        shutil.copyfile(PREBUILT_GAMES, image)
        JOB.write(f"Jeux déjà préparés ({len(games)}) : pas besoin du compilateur ARM.")
        return None
    if games and not have_toolchain():
        return ("Pour ajouter de nouveaux jeux, installe la chaîne ARM "
                "(Arm GNU Toolchain, arm-none-eabi) ou retire les jeux ajoutés.")
    code, out = run([PYTHON, str(LINK_GAMES), str(FIRMWARE_ELF),
                     str(GAMES_DIR), image])
    JOB.write("\n".join(l for l in out.splitlines() if "RWX" not in l))
    if code != 0:
        return "Impossible de préparer les jeux (voir le journal)."
    return None


def install():
    try:
        JOB.step("Recherche de la calculatrice")
        status = calculator_status()
        if not status["connected"]:
            return JOB.fail("Calculatrice introuvable. Branche-la en USB, "
                            "appuie sur Reset (au dos) et attends le menu officiel.")
        JOB.write(f"Modèle: {status['model']}, slot inactif: {status['inactiveSlot']}")
        if status["model"] != "n0120":
            return JOB.fail(f"Modèle {status['model']} non supporté (Windows-OS est fait pour la n0120).")
        if status["inactiveSlot"] != "B":
            return JOB.fail("La calculatrice tourne sur le slot B. Appuie sur Reset au dos "
                            "pour revenir sur le système officiel (slot A), puis réessaie.")
        if not FIRMWARE_DFU.exists() or not FIRMWARE_ELF.exists():
            return JOB.fail("Firmware Windows-OS introuvable dans firmware/ : "
                            "compile-le d'abord (voir le README).")

        JOB.step("Préparation des jeux")
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "games.bin")
            error = prepare_games(image)
            if error:
                return JOB.fail(error)
            apps_start, apps_end = apps_area()
            size = os.path.getsize(image)
            if size > apps_end - apps_start:
                return JOB.fail("Trop de jeux : ils ne tiennent pas dans le slot B.")

            JOB.step("Copie des jeux sur la calculatrice")
            if DFU_UTIL:
                code, out = run([DFU_UTIL, "-a", "0", "-s", hex(apps_start), "-D", image])
            else:
                code, out = dfu("-s", hex(apps_start), "-D", image)
            JOB.write(out)
            if code != 0:
                return JOB.fail("Échec de l'écriture des jeux.")

            JOB.step("Vérification des jeux")
            if DFU_UTIL:
                back = os.path.join(tmp, "readback.bin")
                code, out = run([DFU_UTIL, "-a", "0", "-s", f"{hex(apps_start)}:{size}",
                                 "-U", back])
                if code != 0 or Path(back).read_bytes() != Path(image).read_bytes():
                    JOB.write(out)
                    return JOB.fail("Les jeux relus ne correspondent pas à ce qui a été écrit.")
                JOB.write("Jeux vérifiés : identiques.")
            else:
                JOB.write("Relecture sautée (dfu-util absent) : l'outil de NumWorks "
                          "contrôle l'état de chaque bloc pendant l'écriture.")

        JOB.step("Installation de Windows-OS (slot B)")
        if dfu_query("--inactive_slot") != "B":
            return JOB.fail("Le slot B n'est plus le slot inactif, installation annulée.")
        code, out = dfu("-s", f"{hex(USERLAND_B)}:leave", "-D", str(FIRMWARE_DFU))
        JOB.write(out)
        if code != 0:
            return JOB.fail("Échec de l’écriture de Windows-OS.")

        JOB.step("Windows-OS démarre sur la calculatrice", "done")
        with JOB.lock:
            JOB.result = "ok"
            JOB.running = False
    except Exception as e:  # pragma: no cover - surfaced to the page
        JOB.fail(f"{type(e).__name__}: {e}")


def start_install():
    with JOB.lock:
        if JOB.running:
            return False
        JOB.running = True
        JOB.steps = []
        JOB.log = []
        JOB.result = None
    threading.Thread(target=install, daemon=True).start()
    return True


# --------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self):
        # Basic protection against other websites driving the installer
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or origin == f"http://{host}"

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            busy = JOB.snapshot()["running"]
            calc = {"connected": None} if busy else calculator_status()
            try:
                start, end = apps_area()
                capacity = end - start
            except Exception:
                capacity = 0x260000
            self.send_json({"calculator": calc, "games": list_games(),
                            "firmware": FIRMWARE_DFU.exists(),
                            "appsCapacity": capacity,
                            "platform": "windows" if WINDOWS else "mac"})
        elif self.path == "/api/job":
            self.send_json(JOB.snapshot())
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.same_origin():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/api/install":
            ok = start_install()
            return self.send_json({"started": ok}, 200 if ok else 409)
        if self.path == "/api/games":
            target = safe_game_path(self.headers.get("X-Filename", ""))
            length = int(self.headers.get("Content-Length", "0"))
            if target is None:
                return self.send_json({"error": "Seuls les fichiers .nwa sont acceptés."}, 400)
            if length <= 0 or length > MAX_UPLOAD:
                return self.send_json({"error": "Fichier vide ou trop gros."}, 400)
            data = self.rfile.read(length)
            if not data.startswith(b"\x7fELF"):
                return self.send_json({"error": "Ce n'est pas un fichier .nwa valide."}, 400)
            target.write_bytes(data)
            if game_name(target) is None:
                target.unlink()
                return self.send_json({"error": "Fichier .nwa invalide (pas de nom d'app)."}, 400)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.same_origin():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path.startswith("/api/games/"):
            target = safe_game_path(self.path[len("/api/games/"):])
            if target is None or not target.exists():
                return self.send_json({"error": "Jeu introuvable."}, 404)
            target.unlink()
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    GAMES_DIR.mkdir(exist_ok=True)
    # 8765 / 8766 are the PSP-OS / Switch-OS installers: use another port,
    # and the next free one if this one is already taken.
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Aucun port libre pour l'installateur.")
    url = f"http://127.0.0.1:{port}/"
    print(f"Windows-OS Installer: {url}  (Ctrl+C pour quitter)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
