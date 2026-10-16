#!/bin/sh
# Runs once, when the database volume is created: imports schema.sql with the application account opened to the
# other containers ('%' instead of 'localhost') and given the password from APP_DB_PASSWORD.
set -e
sed -e "s/'routewise_app'@'localhost'/'routewise_app'@'%'/g" \
    -e "s/IDENTIFIED BY 'routewise_app_pw'/IDENTIFIED BY '${APP_DB_PASSWORD}'/" /routewise/schema.sql \
  | mysql -uroot -p"${MYSQL_ROOT_PASSWORD}"
echo "RouteWise schema imported."
