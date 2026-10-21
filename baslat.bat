@echo off
REM Sanel Prototip Takip - Windows'ta hizli baslatma
REM Python 3.9+ kurulu olmali (python.org). Ek paket gerekmez.
cd /d "%~dp0"
set PT_PORT=8080
REM Sirket hesabiyla giris icin asagidaki iki satirin basindaki REM'i kaldirin:
REM set PT_LDAP_URL=ldap://10.34.10.1:389
REM set PT_LDAP_DOMAIN=sanelelk.local
python server.py
pause
