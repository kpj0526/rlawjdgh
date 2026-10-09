"""PyInstaller 진입점: ChromeJumper.exe

- 기본: GUI 실행
- --selftest ...: 배포 파일 점검(chrome_jumper/selftest.py)
창 모드(콘솔 없음) 실행 파일이므로 시작 중 예외는 오류 창과 crash.log로 알린다.
"""

import sys
import traceback


def _crash(exc: BaseException) -> None:
    from datetime import datetime

    from chrome_jumper.config import default_data_dir
    text = "".join(traceback.format_exception(exc))
    try:
        d = default_data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n==== {datetime.now():%Y-%m-%d %H:%M:%S}\n{text}")
    except OSError:
        pass
    try:
        from tkinter import Tk, messagebox
        r = Tk()
        r.withdraw()
        messagebox.showerror("Chrome 점프 자동화 - 오류", f"프로그램을 시작하지 못했습니다.\n\n{exc!r}\n\n"
                             f"자세한 내용: %APPDATA%\\ChromeJumper\\logs\\crash.log")
        r.destroy()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        from chrome_jumper.selftest import run
        return run(sys.argv[1:])
    from chrome_jumper.app import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        _crash(exc)
        code = 1
    sys.exit(code)
