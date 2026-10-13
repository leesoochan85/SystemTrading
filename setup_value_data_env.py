"""Create isolated Windows data venv using an already installed 64bit Python.

Run with the existing Python 3.9. No credentials or system installs are used.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys


PROBE = "import sys,struct,json; print(json.dumps({'exe':sys.executable,'version':list(sys.version_info[:2]),'bits':struct.calcsize('P')*8}))"


def suitable(executable_args):
    try:
        result = subprocess.run(executable_args + ["-c", PROBE], capture_output=True, text=True, timeout=15)
        if result.returncode:
            return None
        info = json.loads(result.stdout)
        if tuple(info["version"]) >= (3, 10) and info["bits"] == 64:
            return info["exe"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def main():
    root = Path(__file__).resolve().parent
    env = root / ".venv-data"
    interpreter = env / "Scripts/python.exe"
    if env.exists():
        if not suitable([str(interpreter)]):
            print("[Setup stopped] Existing .venv-data is unsuitable; it was not changed.")
            return 2
    else:
        selected = suitable([sys.executable])
        launcher = shutil.which("py")
        if not selected and launcher:
            for target in ("-3.12-64", "-3.11-64", "-3.13-64", "-3.10-64", "-3.14-64"):
                selected = suitable([launcher, target])
                if selected:
                    break
        if not selected:
            print("[Setup stopped] Install Python 3.11 or 3.12 64bit, then run this script again.")
            print("Download: https://www.python.org/downloads/windows/")
            return 2
        print("[Data Python]", selected)
        subprocess.run([selected, "-m", "venv", str(env)], check=True)
    subprocess.run([str(interpreter), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(interpreter), "-m", "pip", "install", "-r",
                    str(root / "requirements-value-data.txt")], check=True)
    print("[Setup OK] .venv-data\\Scripts\\python.exe collect_value_fundamentals.py --check-market")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
