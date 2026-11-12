@echo off
REM Allow phones / the ESP32-CAM on your network to reach the backend on TCP port 8000.
REM Right-click this file -> "Run as administrator".
netsh advfirewall firewall add rule name="Door monitor 8000" dir=in action=allow protocol=TCP localport=8000
if errorlevel 1 (
  echo.
  echo FAILED - did you use "Run as administrator"?
) else (
  echo.
  echo Done. Port 8000 is open for incoming connections.
)
pause
