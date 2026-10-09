# PyInstaller 빌드 설정 — build.bat에서 사용. (onedir: 실행이 빠르고 Playwright 드라이버를 매번 풀지 않음)
# Playwright는 자체 PyInstaller 훅으로 드라이버(node.exe + package)를 포함한다. 브라우저는 넣지 않고 시스템 Chrome을 쓴다.
from pathlib import Path

ROOT = Path(SPECPATH).parent
EXCLUDES = ["pytest", "_pytest", "PIL", "numpy", "cv2", "IPython", "pydoc_data", "unittest", "mock_site"]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=["chrome_jumper.selftest"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ChromeJumper",
    console=False,  # 창 모드(콘솔 없음)
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ChromeJumper", upx=False)
