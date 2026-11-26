set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/private-docs/local-environment.sh" ]; then
    . "$script_dir/private-docs/local-environment.sh"
fi
app_root=${EMBRYO_APP_ROOT:-/opt/embryo-erpnext}
cd "$app_root"
site=${EMBRYO_SITE:-embryo.localhost}
export SITE="$site"
docker compose exec -T backend bench --version
test -s administrator-password
set -a
. ./.env
set +a
export EMBRYO_ADMIN_PASSWORD="$(cat administrator-password)"
docker compose exec -T -e DB_PASSWORD -e EMBRYO_ADMIN_PASSWORD -e SITE backend bash -ec '
    if [ ! -f "sites/$SITE/site_config.json" ]; then
        bench new-site --db-root-username root --db-root-password "$DB_PASSWORD" --mariadb-user-host-login-scope="%" --admin-password "$EMBRYO_ADMIN_PASSWORD" --install-app erpnext "$SITE"
    fi
    bench --site "$SITE" list-apps
    bench --site "$SITE" set-config -p developer_mode 1
    bench --site "$SITE" set-config host_name http://127.0.0.1:18085
    bench use "$SITE"
'
unset EMBRYO_ADMIN_PASSWORD DB_PASSWORD SITE
