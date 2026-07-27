CREATE ROLE bibitasks_migrator LOGIN PASSWORD 'local-migration-only'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT CONNECT ON DATABASE bibitasks_migration TO bibitasks_migrator;
ALTER SCHEMA public OWNER TO bibitasks_migrator;
