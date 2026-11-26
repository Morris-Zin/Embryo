@echo off
setlocal
if exist "%~dp0private-docs\local-config.cmd" call "%~dp0private-docs\local-config.cmd"
if not defined EMBRYO_WSL_DISTRO set "EMBRYO_WSL_DISTRO=EmbryoERPNext"
if not defined EMBRYO_COMPOSE_COMMAND set "EMBRYO_COMPOSE_COMMAND=embryo-erp"
wsl.exe -d %EMBRYO_WSL_DISTRO% -u root --cd / --exec systemctl start docker
if errorlevel 1 exit /b 1
wsl.exe -d %EMBRYO_WSL_DISTRO% -u root --cd / --exec %EMBRYO_COMPOSE_COMMAND% up -d
if errorlevel 1 exit /b 1
echo ERPNext is starting at http://127.0.0.1:18085
echo Leave this window open to keep the development distro available.
wsl.exe -d %EMBRYO_WSL_DISTRO% -u root --cd / --exec sleep infinity
