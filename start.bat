@echo off
cd /d "%~dp0"

echo ========================================
echo   Medikiosk - My Pre-Consultation Bot
echo   Same-Network / LAN Mode
echo ========================================
if not defined GROQ_API_KEY (
  set /p GROQ_API_KEY=Enter your NEW Groq API key: 
)
if not defined GROQ_API_KEY (
  echo.
  echo ERROR: GROQ_API_KEY was not entered.
  pause
  exit /b 1
)

echo.
echo Your computer's network addresses:
ipconfig | findstr /i "IPv4"
echo.
echo Starting Medikiosk on ALL network interfaces...
echo Other devices on the same Wi-Fi/LAN can use:
echo   http://YOUR-COMPUTER-IP:5000
 echo.
echo IMPORTANT: If Windows Firewall asks, allow Python on Private networks.
echo Keep this window open while others use the website.
echo.
python app.py
pause
