@echo off
setlocal
if exist "%~dp0private-docs\local-config.cmd" call "%~dp0private-docs\local-config.cmd"
if not defined EMBRYO_WSL_DISTRO set "EMBRYO_WSL_DISTRO=EmbryoERPNext"
if not defined EMBRYO_COMPOSE_COMMAND set "EMBRYO_COMPOSE_COMMAND=embryo-erp"
wsl.exe -d %EMBRYO_WSL_DISTRO% -u root --cd / --exec %EMBRYO_COMPOSE_COMMAND% stop
if errorlevel 1 exit /b 1
wsl.exe --terminate %EMBRYO_WSL_DISTRO%
