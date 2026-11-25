"""Discord VPN — usa a Proton VPN Free (WireGuard) SOMENTE no Discord. Windows."""
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from core_config import (ConfigError, DISCORD_EXES, PROXY_NAME, build_yaml,
                         parse_wireguard)

APP = "DiscordVPN"
DATA = Path(os.environ.get("APPDATA", ".")) / APP
CORE_DIR = DATA / "core"
CORE_EXE_NAME = "discordvpn-core.exe"   # nome próprio: não mata o Clash/mihomo de outros apps
WG_CONF = DATA / "proton.conf"
SETTINGS_FILE = DATA / "settings.json"
API_PORT = 9097
TASK_AUTOSTART = "DiscordVPN-Autostart"
TASK_LAUNCH = "DiscordVPN-LaunchDiscord"
TEST_URL = "https://www.gstatic.com/generate_204"
NO_WINDOW = 0x08000000

DEFAULTS = {"autostart": True, "launch_discord": True, "secret": ""}
COLORS = {"off": (140, 140, 140, 255), "connecting": (240, 180, 0, 255),
          "on": (46, 184, 92, 255), "error": (220, 60, 60, 255)}
LABELS = {"off": "desconectada", "connecting": "conectando…",
          "on": "ativa (só Discord)", "error": "erro"}

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # ignora proxy do sistema


# ---------------------------------------------------------------- utilidades
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="ignore",
                          creationflags=NO_WINDOW, **kw)


def ps(script):
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def ps_q(s):
    return "'" + s.replace("'", "''") + "'"


def msgbox(text, title="Discord VPN", icon=0x40):
    ctypes.windll.user32.MessageBoxW(0, text, title, icon | 0x40000)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def bundled_dir():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "bin"
    return Path(__file__).resolve().parent / "bin"


def launch_cmd():
    """(executável, argumentos) para a tarefa de inicialização."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--autostart"
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else py), f'"{Path(__file__).resolve()}" --autostart'


def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 235))
    return img


def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
    except Exception:
        pass
    if not s["secret"]:
        s["secret"] = secrets.token_hex(16)
    save_settings(s)
    return s


def save_settings(s):
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")


# ------------------------------------------------------- tarefas do Windows
def set_autostart(enable: bool) -> bool:
    """Tarefa agendada no logon, com privilégio máximo (sem UAC) — roda antes do Discord."""
    if not enable:
        run(["schtasks", "/Delete", "/TN", TASK_AUTOSTART, "/F"])
        return True
    exe, args = launch_cmd()
    script = f"""
$u = "$env:USERDOMAIN\\$env:USERNAME"
$a = New-ScheduledTaskAction -Execute {ps_q(exe)} -Argument {ps_q(args)}
$t = New-ScheduledTaskTrigger -AtLogOn -User $u
$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName {ps_q(TASK_AUTOSTART)} -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null
"""
    return ps(script).returncode == 0


def discord_install():
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for folder in ("Discord", "DiscordPTB", "DiscordCanary"):
        upd = base / folder / "Update.exe"
        if upd.exists():
            return upd, f"{folder}.exe"
    return None, None


def discord_running():
    out = run(["tasklist", "/FO", "CSV", "/NH"]).stdout.lower()
    return any(f'"{n.lower()}"' in out for n in DISCORD_EXES)


def kill_discord():
    for n in DISCORD_EXES:
        run(["taskkill", "/F", "/IM", n])


def disable_discord_autostart():
    """Remove a entrada do Discord no Iniciar do Windows: quem abre o Discord é este programa."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
    except OSError:
        return
    with key:
        for name in ("Discord", "DiscordPTB", "DiscordCanary"):
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def launch_unelevated(update_exe: Path, exe_name: str):
    """Abre o Discord SEM privilégio de admin (o app é admin, o Discord não deve ser)."""
    script = f"""
$u = "$env:USERDOMAIN\\$env:USERNAME"
$a = New-ScheduledTaskAction -Execute {ps_q(str(update_exe))} -Argument {ps_q('--processStart ' + exe_name)}
$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {ps_q(TASK_LAUNCH)} -Action $a -Principal $p -Force | Out-Null
"""
    if ps(script).returncode == 0 and run(["schtasks", "/Run", "/TN", TASK_LAUNCH]).returncode == 0:
        return
    subprocess.Popen([str(update_exe), "--processStart", exe_name], creationflags=NO_WINDOW)


# ------------------------------------------------------------------- app
class App:
    def __init__(self):
        self.settings = load_settings()
        self.proc = None
        self.logf = None
        self.state = "off"
        self.msg = ""
        self.want = False
        self.lock = threading.Lock()
        self.quit_evt = threading.Event()
        self.icon = None

    # ---- estado / UI
    def status_text(self):
        return LABELS[self.state] + (f" — {self.msg}" if self.msg and self.state == "error" else "")

    def notify(self, text):
        try:
            if self.icon:
                self.icon.notify(text, "Discord VPN")
        except Exception:
            pass

    def set_state(self, st, msg=""):
        self.state, self.msg = st, msg
        if self.icon:
            try:
                self.icon.icon = make_icon(COLORS[st])
                self.icon.title = f"Discord VPN — {self.status_text()}"[:127]
                self.icon.update_menu()
            except Exception:
                pass
        if msg:
            self.notify(msg)

    # ---- API do núcleo
    def api(self, path, timeout=6):
        req = urllib.request.Request(f"http://127.0.0.1:{API_PORT}{path}",
                                     headers={"Authorization": f"Bearer {self.settings['secret']}"})
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def tunnel_delay(self):
        q = urllib.parse.quote(TEST_URL, safe="")
        return self.api(f"/proxies/{PROXY_NAME}/delay?url={q}&timeout=5000", 9).get("delay", 0)

    # ---- núcleo (mihomo)
    def _start_core(self, wg):
        CORE_DIR.mkdir(parents=True, exist_ok=True)
        self._stop_core()
        run(["taskkill", "/F", "/IM", CORE_EXE_NAME])
        src = bundled_dir()
        for name, dst in (("mihomo.exe", CORE_EXE_NAME), ("wintun.dll", "wintun.dll")):
            s, d = src / name, CORE_DIR / dst
            if not d.exists() or d.stat().st_size != s.stat().st_size:
                shutil.copy2(s, d)
        cfg = CORE_DIR / "config.yaml"
        cfg.write_text(build_yaml(wg, self.settings["secret"], API_PORT), "utf-8")
        self.logf = open(CORE_DIR / "core.log", "wb")
        self.proc = subprocess.Popen(
            [str(CORE_DIR / CORE_EXE_NAME), "-d", str(CORE_DIR), "-f", str(cfg)],
            cwd=str(CORE_DIR), stdout=self.logf, stderr=self.logf, creationflags=NO_WINDOW)

    def _stop_core(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:
                run(["taskkill", "/F", "/PID", str(p.pid)])
        if self.logf:
            try:
                self.logf.close()
            except Exception:
                pass
            self.logf = None

    def _wait_ready(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end and self.want:
            if self.proc is None or self.proc.poll() is not None:
                return False
            try:
                if self.tunnel_delay() > 0:
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    # ---- conectar / desconectar
    def connect(self, relaunch=True):
        self.want = True
        threading.Thread(target=self._connect, args=(relaunch,), daemon=True).start()

    def _connect(self, relaunch):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.state == "on":
                return
            if not WG_CONF.exists():
                self.set_state("off")
                self.import_conf()
                return
            self.set_state("connecting")
            if self.settings["launch_discord"]:
                disable_discord_autostart()
            try:
                self._start_core(parse_wireguard(WG_CONF.read_text("utf-8-sig")))
            except Exception as e:
                self.set_state("error", str(e))
                return
            if self._wait_ready():
                self.set_state("on")
                self.notify("VPN ativa — apenas o Discord usa a Proton.")
                if self.settings["launch_discord"] and relaunch:
                    self.launch_discord(restart=True)
            else:
                self._stop_core()
                if self.want:
                    self.set_state("error", "Não consegui conectar. Veja core.log em %APPDATA%\\DiscordVPN\\core")
                else:
                    self.set_state("off")
        finally:
            self.lock.release()

    def disconnect(self):
        self.want = False
        self._stop_core()
        self.set_state("off")
        self.notify("VPN desligada. O Discord agora usa sua conexão normal.")

    def launch_discord(self, restart=False):
        if self.state != "on":
            self.notify("Conecte a VPN antes de abrir o Discord.")
            return
        upd, exe = discord_install()
        if not upd:
            self.notify("Discord não encontrado.")
            return
        if discord_running():
            if not restart:
                return
            kill_discord()      # conexões abertas antes da VPN precisam ser refeitas
            time.sleep(2)
        launch_unelevated(upd, exe)

    # ---- monitor
    def monitor(self):
        fails = 0
        while not self.quit_evt.wait(10):
            if self.state != "on" or not self.want:
                fails = 0
                continue
            dead = self.proc is None or self.proc.poll() is not None
            if not dead:
                try:
                    self.tunnel_delay()
                    fails = 0
                    continue
                except Exception:
                    fails += 1
            if dead or fails >= 3:
                fails = 0
                self._stop_core()
                self.set_state("error", "VPN caiu — reconectando (Discord fica sem rede, sem vazar IP)")
                self.connect(relaunch=False)

    # ---- diagnóstico
    def diagnose(self):
        def work():
            try:
                conns = self.api("/connections")["connections"]
            except Exception:
                msgbox("A VPN não está ativa.", icon=0x30)
                return
            mine = [c for c in conns if "discord" in
                    (c["metadata"].get("process", "") + c["metadata"].get("processPath", "")).lower()]
            via = [c for c in mine if PROXY_NAME in c.get("chains", [])]
            out = [c for c in mine if PROXY_NAME not in c.get("chains", [])]
            text = (f"Conexões do Discord agora: {len(mine)}\n"
                    f"  • pela VPN: {len(via)}\n  • diretas (fora da VPN): {len(out)}")
            if out:
                dests = sorted({f"{c['metadata'].get('destinationIP', '?')}:{c['metadata'].get('destinationPort', '')}"
                                for c in out})[:6]
                text += "\n\nDiretas:\n" + "\n".join(dests)
            msgbox(text)
        threading.Thread(target=work, daemon=True).start()

    # ---- importar .conf
    def import_conf(self):
        def work():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="Selecione o .conf WireGuard baixado da Proton VPN",
                filetypes=[("WireGuard", "*.conf"), ("Todos", "*.*")])
            root.destroy()
            if not path:
                return
            try:
                text = Path(path).read_text("utf-8-sig")
                parse_wireguard(text)
            except (ConfigError, OSError, UnicodeDecodeError) as e:
                msgbox(f"Não consegui usar esse arquivo:\n{e}", icon=0x10)
                return
            DATA.mkdir(parents=True, exist_ok=True)
            WG_CONF.write_text(text, "utf-8")
            if self.settings["autostart"]:
                set_autostart(True)
            if self.state in ("on", "connecting", "error"):
                self.disconnect()
            self.connect()
        threading.Thread(target=work, daemon=True).start()

    # ---- bandeja
    def toggle(self, key):
        def action(icon, item):
            self.settings[key] = not self.settings[key]
            save_settings(self.settings)
            if key == "autostart":
                threading.Thread(target=set_autostart, args=(self.settings[key],), daemon=True).start()
            icon.update_menu()
        return action

    def quit(self, icon, item):
        self.quit_evt.set()
        self.want = False
        self._stop_core()
        icon.stop()

    def build_menu(self):
        M, I = pystray.MenuItem, pystray.Menu
        return I(
            M(lambda item: f"Status: {self.status_text()}", None, enabled=False),
            M("Conectar", lambda icon, item: self.connect(),
              visible=lambda item: self.state in ("off", "error")),
            M("Desconectar", lambda icon, item: self.disconnect(),
              visible=lambda item: self.state in ("on", "connecting")),
            M("Abrir Discord", lambda icon, item: threading.Thread(
                target=self.launch_discord, daemon=True).start()),
            M("Diagnóstico (conexões do Discord)", lambda icon, item: self.diagnose()),
            I.SEPARATOR,
            M("Importar .conf WireGuard (Proton)…", lambda icon, item: self.import_conf()),
            M("Iniciar com o Windows", self.toggle("autostart"),
              checked=lambda item: self.settings["autostart"]),
            M("Este programa abre o Discord após a VPN", self.toggle("launch_discord"),
              checked=lambda item: self.settings["launch_discord"]),
            I.SEPARATOR,
            M("Sair", self.quit),
        )

    def _setup(self, icon):
        icon.visible = True
        threading.Thread(target=self.monitor, daemon=True).start()
        if WG_CONF.exists():
            self.connect()
            if self.settings["autostart"] and "--autostart" not in sys.argv:
                threading.Thread(target=set_autostart, args=(True,), daemon=True).start()
        else:
            self.import_conf()

    def run(self):
        self.icon = pystray.Icon(APP, make_icon(COLORS["off"]), "Discord VPN", self.build_menu())
        self.icon.run(setup=self._setup)


def main():
    if not is_admin():  # necessário para criar o adaptador de rede (TUN)
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        if not getattr(sys, "frozen", False):
            params = f'"{Path(__file__).resolve()}" {params}'
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\DiscordVPN_single_instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # já está rodando
        return
    DATA.mkdir(parents=True, exist_ok=True)
    App().run()


if __name__ == "__main__":
    main()
