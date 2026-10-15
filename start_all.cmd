@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  Team 9 - Adaptive Traffic Signal Controller
echo  Starting the local demo stack...
echo ============================================================
echo.

where.exe py.exe >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python Launcher was not found. Install Python 3.13 and try again.
  goto :failed
)
where.exe node-red.cmd >nul 2>&1
if errorlevel 1 (
  echo ERROR: node-red.cmd was not found. Install Node-RED or add it to PATH.
  goto :failed
)
where.exe npm.cmd >nul 2>&1
if errorlevel 1 (
  echo ERROR: npm.cmd was not found. Install Node.js and try again.
  goto :failed
)

set "NODERED_USER_DIR=%USERPROFILE%\.node-red-team9"
set "PROJECT_DIR=%~dp0"
if not exist "%NODERED_USER_DIR%" mkdir "%NODERED_USER_DIR%"
if not exist "%NODERED_USER_DIR%\flows.json" (
  echo Initializing the Team 9 Node-RED profile...
  copy /Y "%~dp0node_red\flows.json" "%NODERED_USER_DIR%\flows.json" >nul
  if errorlevel 1 (
    echo ERROR: Could not copy the project Node-RED flow.
    goto :failed
  )
)
node.exe -e "const fs=require('fs'),path=require('path');const file=path.join(process.env.NODERED_USER_DIR,'flows.json');const source=path.join(process.env.PROJECT_DIR,'node_red','flows.json');let flows;try{flows=JSON.parse(fs.readFileSync(file,'utf8'));}catch{}if(!Array.isArray(flows)){flows=JSON.parse(fs.readFileSync(source,'utf8'));}const db=flows.find(n=>n.type==='sqlitedb');if(!db)process.exit(2);db.db=path.join(process.env.PROJECT_DIR,'data','traffic.db');fs.writeFileSync(file,JSON.stringify(flows,null,4)+'\n');"
if errorlevel 1 (
  echo ERROR: Could not configure a valid Node-RED flow and project database path.
  goto :failed
)

if not exist "%NODERED_USER_DIR%\node_modules\node-red-dashboard\package.json" (
  echo Installing required Node-RED dashboard and SQLite nodes...
  call npm.cmd install --prefix "%NODERED_USER_DIR%" node-red-dashboard node-red-node-sqlite
  if errorlevel 1 (
    echo ERROR: Node-RED node installation failed. Check your internet connection and try again.
    goto :failed
  )
)

py -3.13 -c "import paho.mqtt.client" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python 3.13 or the project's MQTT dependencies are unavailable.
  echo Install the project requirements, then click this file again.
  goto :failed
)

rem Start Mosquitto if it is not already listening. The Windows service needs UAC elevation.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ok=Test-NetConnection 127.0.0.1 -Port 1883 -InformationLevel Quiet -WarningAction SilentlyContinue; if($ok){exit 0}; $svc=Get-Service -Name mosquitto -ErrorAction SilentlyContinue; if(-not $svc){exit 2}; if($svc.Status -ne 'Running'){ $p=Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command Start-Service -Name mosquitto'; if($p.ExitCode -ne 0){exit $p.ExitCode} }; exit 0"
if errorlevel 1 (
  echo ERROR: Mosquitto could not be started. Approve the Windows elevation prompt and check the service.
  goto :failed
)

echo Waiting for Mosquitto on port 1883...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$end=(Get-Date).AddSeconds(45); do { if(Test-NetConnection 127.0.0.1 -Port 1883 -InformationLevel Quiet -WarningAction SilentlyContinue){exit 0}; Start-Sleep -Seconds 1 } while((Get-Date) -lt $end); exit 1"
if errorlevel 1 (
  echo ERROR: Mosquitto did not open port 1883.
  goto :failed
)

rem Reuse only a Node-RED instance that has this project's flow deployed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-RestMethod -Uri 'http://127.0.0.1:1880/flows' -TimeoutSec 3; if($r|Where-Object {$_.type -eq 'tab' -and $_.label -eq 'Team 9 Traffic'}){exit 0}; exit 2}catch{}; exit 1"
if errorlevel 2 (
  echo ERROR: Another Node-RED flow is using port 1880. Stop it and retry this launcher.
  goto :failed
)
if errorlevel 1 (
  echo Starting Node-RED...
  start "Team 9 - Node-RED" node-red.cmd --userDir "%NODERED_USER_DIR%"
)

echo Waiting for Node-RED on port 1880...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$end=(Get-Date).AddSeconds(60); do { try{$r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:1880/flows' -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}}catch{}; Start-Sleep -Seconds 1 } while((Get-Date) -lt $end); exit 1"
if errorlevel 1 (
  echo ERROR: Node-RED did not become ready. Check its window and the installed dashboard/SQLite nodes.
  goto :failed
)

rem Each process check avoids duplicate MQTT client IDs when the launcher is clicked again.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process|Where-Object {$_.Name -like 'python*' -and $_.CommandLine -match 'src\.actuator\.actuator_node'}; if($p){exit 0}else{exit 1}"
if errorlevel 1 (
  echo Starting actuator...
  start "Team 9 - Actuator" py.exe -3.13 -u -m src.actuator.actuator_node
) else (
  echo Actuator is already running.
)

timeout /t 2 /nobreak >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process|Where-Object {$_.Name -like 'python*' -and $_.CommandLine -match 'src\.service\.controller_service'}; if($p){exit 0}else{exit 1}"
if errorlevel 1 (
  echo Starting controller...
  start "Team 9 - Controller" py.exe -3.13 -u -m src.service.controller_service
) else (
  echo Controller is already running.
)

timeout /t 2 /nobreak >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process|Where-Object {$_.Name -like 'python*' -and $_.CommandLine -match 'src\.mqtt\.publisher'}; if($p){exit 0}else{exit 1}"
if errorlevel 1 (
  echo Starting dataset publisher...
  start "Team 9 - Publisher" py.exe -3.13 -u -m src.mqtt.publisher --speed 60 --limit 0
) else (
  echo Publisher is already running.
)

echo.
echo Demo stack is ready.
echo Dashboard (primary): http://127.0.0.1:1880/dashboard
echo Component view (secondary): http://127.0.0.1:1880/ui/
echo Keep the service windows open while using the demo.
echo Mosquitto may show a Windows UAC prompt if its service was stopped.
timeout /t 8
exit /b 0

:failed
echo.
echo Startup stopped. Fix the message above and click start_all.cmd again.
pause
exit /b 1
