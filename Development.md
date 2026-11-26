# Embryo ERPNext development site

Open http://127.0.0.1:18085/login and sign in as Administrator. To copy the generated development password from PowerShell, run:

    (wsl.exe -d EmbryoERPNext -u root --exec cat /opt/embryo-erpnext/administrator-password).Trim() | Set-Clipboard

`Start ERPNext.cmd` starts the development stack. Leave its window open while working. `Stop ERPNext.cmd` stops the services and their dedicated WSL distro. Both actions preserve the database and site files.

## Edit and inspect

Open the running site's ERPNext source folder in Windows Explorer:

    \wsl.localhost\EmbryoERPNext\srv\embryo-dev\apps\erpnext

The source tree comes from the installed release image. All application services mount the same tree. After editing Python code, restart the relevant services. Changes to DocTypes, database schemas and bundled browser assets can require a migration or an asset build. For upgradeable Embryo features, develop a separate Frappe application and include it in the image.

PowerShell commands:

    wsl -d EmbryoERPNext -u root --exec embryo-erp ps
    wsl -d EmbryoERPNext -u root --exec embryo-erp exec backend bench --site embryo.localhost console
    wsl -d EmbryoERPNext -u root --exec embryo-erp restart backend queue-short queue-long scheduler
    wsl -d EmbryoERPNext -u root --exec embryo-erp exec backend bench --site embryo.localhost list-apps

## Storage

The WSL virtual disk under `E:\Embryo-ERPNext\Linux` holds the Linux source, MariaDB, Docker images and persistent site files. Compose configuration and private development credentials live under `/opt/embryo-erpnext` in the distro. The database and Redis are on the internal Docker network. The web service publishes only to `127.0.0.1:18085`.

Business onboarding is a separate step. At first sign-in, use the wizard to choose the company's details and financial year for the pilot.

## Local names and overrides

The scripts use these public, neutral defaults:

- WSL distro: `EmbryoERPNext`
- Compose command: `embryo-erp`
- Compose directory: `/opt/embryo-erpnext`
- Development source root: `/srv/embryo-dev`
- Site: `embryo.localhost`

If a local installation uses different names, set `EMBRYO_WSL_DISTRO`, `EMBRYO_COMPOSE_COMMAND`, `EMBRYO_APP_ROOT`, `EMBRYO_DEV_SOURCE_ROOT`, or `EMBRYO_SITE` in the local environment. Keep machine-specific values and credentials in `private-docs/` or outside the repository; never commit them.

Upstream configuration: https://github.com/frappe/frappe_docker
