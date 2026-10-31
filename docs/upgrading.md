# Upgrading

celery-manager is an alpha, so pass `--pre` to pip and read the
[Changelog](changelog.md) before moving between versions. APIs may still change
before 1.0; the [stability policy](stability.md) says what is covered.

## Standard upgrade

1. **Back up** the celery-manager database if its history matters to you.
2. **Install** the new version in every environment that runs it (dashboard,
   workers, beat, events listener):

    ```bash
    pip install --pre --upgrade celery-manager
    ```

3. **Migrate.** By default `init_db()` upgrades the schema when the first
   process starts, so a restart is enough. If you run with
   `auto_migrate=False`, run the migration as a deploy step first:

    ```bash
    celery-manager migrate --db-url "$CELERY_MANAGER_DB_URL" --check   # exit 1 if behind
    celery-manager migrate --db-url "$CELERY_MANAGER_DB_URL"
    ```

4. **Restart** all processes, then run `celery-manager doctor`.

The database user needs DDL permission when a release adds a schema revision.
Full details, including the adoption of databases created by early alphas, are in
[Schema and upgrades](configuration.md#schema-and-upgrades).

## Rolling back

Downgrades are not wrapped in a command, and an older release against a newer
schema is unsupported. Restore the backup you took in step 1 and reinstall the
previous version.

## Version notes

- **0.3:** see the [0.3 release guide](release-0.3.md). Browser sessions are
  server-side now; rotate `secret_key` on upgrade to invalidate old cookies
  (see [Browser sessions](auth-sessions.md#upgrade-compatibility)).
- Every release lists schema changes and breaking changes in the
  [Changelog](changelog.md).
