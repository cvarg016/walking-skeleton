@echo off
:: Force script to run inside the folder where run.bat is located
cd /d "%~dp0"

title Starting Flask Server and Tunnel

echo Starting Flask server...
:: Launch Flask in its own window so errors stay visible
start "Flask Server" cmd /k "python app.py"

:: Give Flask 3 seconds to boot up before opening the tunnel
timeout /t 3 /nobreak >nul

echo Launching Cloudflare Tunnel...
:: Launch Cloudflare Tunnel in its own window (change 5000 to 8080 if using Waitress)
start "Cloudflare Tunnel" cmd /k "cloudflared.exe tunnel --url http://localhost:5000"

echo.
echo Server and Tunnel launched! Keep both windows open.
pause