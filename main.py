"""더블클릭/한 줄 명령 실행용 런처.

사용:
    python main.py            ← Streamlit UI 실행 (브라우저 자동 오픈)
    python main.py --port 8765
    python main.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI_APP = ROOT / "ui" / "app.py"
SRC = ROOT / "src"


def _check_or_install_hint() -> None:
    try:
        import streamlit  # noqa: F401
        return
    except ImportError:
        print(
            "\n❌ Streamlit이 설치돼 있지 않습니다.\n\n"
            "다음 한 줄을 실행해 필요한 패키지를 설치한 뒤 다시 시도하세요:\n\n"
            f"   pip install -r {ROOT / 'ui' / 'requirements.txt'}\n",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    _check_or_install_hint()

    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    sep = ";" if os.name == "nt" else ":"
    env["PYTHONPATH"] = f"{SRC}{sep}{ROOT / 'ui'}{sep}{pp}".rstrip(sep)

    streamlit_cli = shutil.which("streamlit") or sys.executable
    if streamlit_cli == sys.executable:
        cmd = [sys.executable, "-m", "streamlit", "run", str(UI_APP)]
    else:
        cmd = [streamlit_cli, "run", str(UI_APP)]

    cmd += ["--server.port", str(args.port), "--browser.gatherUsageStats", "false"]
    if args.no_browser:
        cmd += ["--server.headless", "true"]

    print("▶ UI를 시작합니다… (브라우저가 자동으로 열려요)")
    print("  종료는 이 창에서 Ctrl+C 입니다.")
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
