@echo off
setlocal
if exist "%~dp0private-docs\local-config.cmd" call "%~dp0private-docs\local-config.cmd"
if not defined EMBRYO_WSL_DISTRO set "EMBRYO_WSL_DISTRO=EmbryoERPNext"
if not defined EMBRYO_APP_ROOT set "EMBRYO_APP_ROOT=/opt/embryo-erpnext"
powershell.exe -NoProfile -Command "$value = wsl.exe -d %EMBRYO_WSL_DISTRO% -u root --exec cat %EMBRYO_APP_ROOT%/administrator-password; if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($value)) { exit 1 }; $value.Trim() | Set-Clipboard"
