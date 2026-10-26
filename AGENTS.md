# Instructions for contributors and coding agents

This is the generic public-source application. Organizations come only from
configuration; never introduce entity names, fixed periods, real fiscal values,
local absolute paths, or historical production data into code or fixtures.

- Never print, commit, or quote API access values from local configuration.
- Use only synthetic fixtures. Do not read private files for ordinary tests.
- No live CloudKassir requests without explicit approval immediately before them.
- Preserve --send, --confirm SEND_CORRECTIONS, positive --limit and demo guards.
- Preserve the initial one-receipt test per organization and final processed check.
- A timeout is an unknown result, not permission to resend.
- Do not reset, replace or delete a working journal to resolve an error.
- Preserve stable entity_id and receipt_id. Do not introduce silent ID migrations.
- Correct source data or processing logic; do not manually fix generated reports.
- Do not add automatic discounts, guessed contact details or fiscal settings.
- Keep the web UI offline and bound to 127.0.0.1.
- Run python -B -m unittest discover -s tests -v after relevant code changes.
- Update input-format documentation and tests with any schema changes.
- Keep inputs, reports, credentials and databases out of source control.

The database schema is distinct from older tools. Do not treat an incompatible
database as a fresh installation. There are no production data or credentials in
this repository, and their absence is expected.
