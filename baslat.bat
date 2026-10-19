@echo off
REM Sanel Prototip Takip - Windows'ta hizli baslatma
REM Python 3.9+ kurulu olmali (python.org). Ek paket gerekmez.
cd /d "%~dp0"
set PT_PORT=8080
python server.py
pause
