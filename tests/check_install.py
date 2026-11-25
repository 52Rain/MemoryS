"""Build and smoke-test a non-editable install from outside the checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def main():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        env_dir = root / "venv"
        venv.create(env_dir, with_pip=True)
        scripts = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        mem = scripts / ("mem.exe" if os.name == "nt" else "mem")
        env = dict(os.environ, MEMORYS_CONFIG_DIR=str(root / "config"), MEMORYS_APP_DATA=str(root / "data"),
                   MEMORYS_VAULT="", MEMORYS_UI_DIR="", PYTHONUTF8="1", PYTHONNOUSERSITE="1")
        env.pop("PYTHONPATH", None)

        def run(*args):
            return subprocess.check_output(args, cwd=sys.base_prefix, env=env, text=True, encoding="utf-8")

        print(run(python, "-m", "pip", "install", "--no-deps", "--no-cache-dir", str(repo)))
        installed = Path(run(python, "-I", "-c",
                             "from runtime import web_server as w; print(w._RUNTIME); "
                             "assert w._find_ui_dir() == w._RUNTIME / 'ui'").strip())
        assert installed.is_relative_to(env_dir.resolve()), installed
        template = repo / "examples" / "demo-vault"
        def files(root):
            return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert len(files(template)) == 9
        assert files(installed / "demo_vault") == files(template)
        assert (installed / "ui_index.html").read_bytes() == (repo / "desktop-app/public/index.html").read_bytes()

        print(run(mem, "init", str(root / "demo"), "--demo"))
        for day, version in [("2026-05-01", "v1"), ("2026-07-01", "v2")]:
            hits = json.loads(run(mem, "search", "--project", "acme-billing", "--as-of", day))
            assert [card["id"] for card in hits] == [f"d-acme-billing-grace-{version}"]
        trace = json.loads(run(mem, "trace", "d-acme-billing-grace-v2"))
        assert "14 days" in trace["events"][0]["text"]
        print("Installed service, HTML, UI assets, 9 demo files, historical search and trace passed.")


if __name__ == "__main__":
    main()
