# QA·점검용 모의 사이트 실행 파일(콘솔). 사용자 배포 ZIP과 별도 ZIP으로 나간다.
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "mock_launcher.py")],
    pathex=[str(ROOT)],
    excludes=["tkinter", "playwright", "chrome_jumper", "pytest", "PIL", "numpy", "cv2"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MockSite", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="MockSite", upx=False)
