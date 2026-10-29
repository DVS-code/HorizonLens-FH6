@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set "APP=%~dp0HorizonLens.exe"
set "MTX=%~dp0_internal\vendor\mediamtx\mediamtx.exe"
netsh advfirewall firewall delete rule name="HorizonLens" >nul 2>&1
netsh advfirewall firewall delete rule name="HorizonLens media" >nul 2>&1
netsh advfirewall firewall add rule name="HorizonLens" dir=in action=allow program="%APP%" profile=private enable=yes >nul
netsh advfirewall firewall add rule name="HorizonLens media" dir=in action=allow program="%MTX%" profile=private enable=yes >nul
echo.
echo HorizonLens is allowed on Private networks.
echo If your phone still can't connect, set your Wi-Fi to Private in
echo Windows Settings ^> Network ^& internet ^> Wi-Fi ^> your network.
echo.
pause
