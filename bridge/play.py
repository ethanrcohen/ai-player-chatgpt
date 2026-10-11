"""Launch local Steam Factorio and its bridge together on macOS."""
import socket
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from .config import ConfigLoader


def reachable(cfg):
    try:
        with socket.create_connection((cfg.rcon.host, cfg.rcon.port), timeout=1):
            return True
    except OSError:
        return False


def configure_local_rcon(path, cfg):
    """Menu-hosted multiplayer uses these settings, not server CLI flags."""
    text = path.read_text()
    match = re.search(r"(?ms)^\[other\]\s*\n(.*?)(?=^\[|\Z)", text)
    if not match:
        raise RuntimeError('Factorio config is missing its [other] section.')
    body = match.group(1)
    for key, value in {'local-rcon-socket': f'127.0.0.1:{cfg.rcon.port}',
                       'local-rcon-password': cfg.rcon.password}.items():
        pattern = r'(?m)^\s*;?\s*' + key + r'=[^\n]*$'
        line = key + '=' + value
        if re.search(pattern, body):
            body = re.sub(pattern, lambda _: line, body)
        else:
            body += '\n' + line + '\n'
    updated = text[:match.start(1)] + body + text[match.end(1):]
    if updated != text:
        backup = path.with_name(path.name + '.before-ai-rcon')
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(updated)


def main():
    cfg = ConfigLoader().get()
    binary = Path.home() / 'Library/Application Support/Steam/steamapps/common/Factorio/factorio.app/Contents/MacOS/factorio'
    if not binary.exists():
        sys.exit('Steam Factorio was not found at the expected macOS location.')
    if subprocess.run(['pgrep', '-x', 'factorio'], stdout=subprocess.DEVNULL).returncode == 0:
        sys.exit('Save and quit Factorio first, then run this launcher again.')
    if cfg.rcon.host not in ('localhost', '127.0.0.1'):
        sys.exit('This launcher requires local RCON (localhost or 127.0.0.1).')
    if not cfg.rcon.password:
        sys.exit('Set FACTORIO_RCON_PASSWORD in bridge/.env first.')
    configure_local_rcon(Path.home() / 'Library/Application Support/factorio/config/config.ini', cfg)
    game = subprocess.Popen([str(binary), '--rcon-bind', f'127.0.0.1:{cfg.rcon.port}',
                             '--rcon-password', cfg.rcon.password],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bridge = None
    try:
        print('Factorio started. Host your saved game through Multiplayer. Waiting for the AI connection…', flush=True)
        while game.poll() is None:
            if bridge is None and reachable(cfg):
                print('Game connection ready; starting the AI bridge.', flush=True)
                bridge = subprocess.Popen([sys.executable, '-m', 'bridge.main'], cwd=Path(__file__).resolve().parents[1])
            if bridge is not None and bridge.poll() is not None:
                print('The AI bridge stopped. Check its error above. Factorio will stay open.', flush=True)
                return
            time.sleep(1)
    except KeyboardInterrupt:
        print('Stopping the AI bridge. Factorio stays open so you can save.')
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.terminate()
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait()


if __name__ == '__main__':
    main()
