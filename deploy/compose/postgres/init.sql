-- SwiftFlow database bootstrap. Runs once, on the Postgres container's first start
-- (docker-entrypoint-initdb.d), as the superuser `swiftflow`.
--
-- Creates one login role and one schema per domain service. Passwords are read
-- from the container environment with psql's \getenv, so no secret lives in this
-- file. (On a managed host such as Supabase, replace the three \getenv lines with
-- literal passwords in the SQL editor.)

\set ON_ERROR_STOP on
\getenv order_pw ORDER_DB_PASSWORD
\getenv inventory_pw INVENTORY_DB_PASSWORD
\getenv notification_pw NOTIFICATION_DB_PASSWORD

-- Roles: plain logins. Not superuser and NOBYPASSRLS, so row-level security applies.
CREATE ROLE order_svc        LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'order_pw';
CREATE ROLE inventory_svc    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'inventory_pw';
CREATE ROLE notification_svc LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'notification_pw';

-- Nobody gets anything by default.
REVOKE ALL ON DATABASE swiftflow FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE swiftflow TO order_svc, inventory_svc, notification_svc;

-- One schema per service, owned by that service's role. Other roles have no USAGE.
CREATE SCHEMA orders        AUTHORIZATION order_svc;
CREATE SCHEMA inventory     AUTHORIZATION inventory_svc;
CREATE SCHEMA notifications AUTHORIZATION notification_svc;

ALTER ROLE order_svc        SET search_path = orders;
ALTER ROLE inventory_svc    SET search_path = inventory;
ALTER ROLE notification_svc SET search_path = notifications;

-- Each service applies its own migrations at start-up (services/<svc>/migrations).
