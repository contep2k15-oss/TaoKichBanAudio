@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ============================================================
    echo   Chua thay moi truong ao "venv". Hay cai dat lan dau bang:
    echo     python -m venv venv
    echo     venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo   (xem README.md de biet chi tiet)
    echo ============================================================
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo Dang mo Silent Video Voiceover Creator...
echo (Mot tab trinh duyet se tu mo. DUNG dong cua so den nay khi dang dung tool.)
streamlit run app_streamlit.py

pause
