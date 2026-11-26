# Embryo

Embryo ERPNext development environment for Windows and WSL.

This repository contains the helper scripts, operating notes, Docker Compose configuration, and the upstream `frappe_docker` reference tree used by the local development environment.

## Contents

- `Development.md` — day-to-day development, access, and maintenance commands.
- `Start ERPNext.cmd` / `Stop ERPNext.cmd` — start and stop the local ERPNext stack.
- `Copy ERPNext password.cmd` — copy the generated local administrator password to the clipboard.
- `Initialize-Site.sh` — initialize the development site inside the WSL distro.
- `Mount-Dev-Source.sh` — mount the editable ERPNext source tree into the application services.
- `runtime-config/` — sanitized copy of the Compose configuration used inside the WSL distro.
- `frappe-config/` — extracted upstream `frappe_docker` reference configuration.
- `private-docs/` — local-only notes and secrets. This folder is ignored and is never pushed.

## Local-only artifacts

The repository intentionally excludes large machine-specific runtime artifacts, including the WSL virtual disk, exported WSL image, downloaded source archive, local `.env` files, private notes, and the Windows shortcut. These files remain on the local machine and are listed in `.gitignore`.

## Development site

After starting the environment, open <http://127.0.0.1:18085/login>. See `Development.md` for the full workflow.
