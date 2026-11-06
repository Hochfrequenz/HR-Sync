# HR-Sync

![Unittests status badge](https://github.com/Hochfrequenz/HR-Sync/workflows/Unittests/badge.svg)
![Coverage status badge](https://github.com/Hochfrequenz/HR-Sync/workflows/Coverage/badge.svg)
![Linting status badge](https://github.com/Hochfrequenz/HR-Sync/workflows/Linting/badge.svg)
![Formatting status badge](https://github.com/Hochfrequenz/HR-Sync/workflows/Formatting/badge.svg)

HR-Sync rolls out the master data of new employees from [Personio](https://www.personio.de/) to
[weclapp](https://www.weclapp.com/) and [decidalo](https://decidalo.de/).
The employee number is the key that links an employee across the three systems.

> [!NOTE]
> HR-Sync is under development. The design and the work packages are described in the
> [implementation plan](docs/plans/2026-10-28-umsetzungsplan.md) (German).

## How It Works

1. Load the employees from Personio, weclapp and Decidalo, one system after the other, and map them onto a common
   data model.
2. Compare the data and create a plan with the operations for weclapp and Decidalo per employee.
   Employees that are missing in a target system are created; existing employees are not changed, deviations are only
   logged.
3. Execute the plan, unless it is empty or HR-Sync runs as a dry run.

Every step is logged as a wide event.

Every transferred field is mandatory: first and last name, e-mail address, employee number, entry date, career level,
mobile phone number, office and weekly hours. Career level, office and weekly hours must be part of fixed mappings,
and the phone number must be convertible to the international form. An employee with a missing or unknown value is
not created in any system; instead, HR-Sync logs a notice with the name of the field, e.g.
`reason=missing_field detail=career_level`.

Records that are known to have no employee number, e.g. technical users, would yield the same notice in every run. To
silence them, list their ids, separated by commas, in `PERSONIO_IGNORED_PERSON_IDS`, `WECLAPP_IGNORED_USER_IDS` or
`DECIDALO_IGNORED_USER_IDS`. Only their notices are dropped, counted as `ignored_notices` in the event `sync.load`:
their e-mail addresses still count, and a listed record with a valid employee number is still compared, so that the
list can't lead to a duplicate.

HR-Sync uses these clients for the APIs:

- [`personio-client`](https://github.com/Hochfrequenz/personio_client.py)
- [`weclapp-client`](https://github.com/Hochfrequenz/weclapp_client.py)
- [`decidalo-client`](https://github.com/Hochfrequenz/decidalo_client.py)

## Usage

Copy [`.env.example`](.env.example) to `.env` and fill in the credentials. Environment variables take precedence over
the `.env` file. Then run:

```bash
uv run hr-sync --dry-run --target decidalo
```

| Option | Meaning |
|---|---|
| `--dry-run` | load and plan, but don't write to any target system |
| `--send-report` | send the report also in a dry run, marked as such; real runs send it if `HR_SYNC_REPORT_TO` is set |
| `--only EMPLOYEE_NUMBER ...` | restrict the plan to these employee numbers; all employees are loaded anyway, to find data problems |
| `--max-employees N` | stop planning as soon as N employees have operations, e.g. `1` for a small test run; the employees after them are not planned |
| `--target weclapp\|decidalo ...` | the target systems (default: all); the others are neither loaded nor written |
| `--env-file PATH` | read the configuration from this file instead of `.env` in the current directory |
| `--log-format pretty\|json` | the format of the events (default: `HR_SYNC_LOG_FORMAT`, otherwise `pretty`) |

| Exit code | Meaning |
|---|---|
| 0 | success, also with an empty plan or with notices |
| 1 | at least one operation failed or was skipped, or the report couldn't be sent |
| 2 | aborted, e.g. because of the configuration or an error while loading |

> [!WARNING]
> There is no weclapp test tenant: a run with weclapp as target writes to production, every new user costs a license
> and gets weclapp's invitation mail. Start with `--dry-run`, and run the first real run only with `--only` and the
> employee number of a test person.

weclapp needs `WECLAPP_API_TOKEN`, `WECLAPP_TENANT` (or `WECLAPP_BASE_URL`) and
`WECLAPP_EMPLOYEE_NUMBER_ATTRIBUTE_ID`, the id of the custom attribute definition with the employee number. Before
planning, HR-Sync checks that the tenant has the custom attributes and work schedule profiles it writes; otherwise the
run aborts with `problem=weclapp_setup_mismatch` and their ids. Their ids, the licenses, roles and the cost rate of new
users are fixed values of Hochfrequenz, so other companies can't use weclapp as a target yet. New users get the
mobile phone number in the form `+49 151 46414222`.

After creating a user and its employee record, HR-Sync invites the user, so that weclapp sends the invitation mail.
weclapp has no dry run for this, so only a run without `--dry-run` invites. A user that existed already, e.g. one
whose employee record a run completes, isn't invited. If the invitation fails, the operation ends with
`detail=invitation_not_sent` and isn't repeated by later runs: invite the user by hand.

### Report

At the end of a run, HR-Sync sends a short report to the HR management, through the HTTP API of
[Mailgun](https://www.mailgun.com/). It's written in German and lists the employees it created and in which systems,
problems while creating, data problems in the systems and existing employees with deviations, or why the run was
aborted. Like the events, it contains no personal data besides employee numbers and the ids of records without one. A
run with nothing to report sends no mail.

| Variable | Meaning |
|---|---|
| `HR_SYNC_REPORT_TO` | the recipients, separated by commas; without recipients, no report is sent |
| `HR_SYNC_REPORT_FROM` | the sender, e.g. `HR-Sync <hr-sync@mg.example.com>` |
| `MAILGUN_API_KEY`, `MAILGUN_DOMAIN` | the API key and the sending domain in Mailgun |
| `MAILGUN_BASE_URL` | default `https://api.eu.mailgun.net`; `https://api.mailgun.net` for an account in the US |

A dry run sends the report only with `--send-report`, marked as a dry run, e.g. to test the setup. The test mode never
sends one. If the mail can't be sent, the sync is done anyway, the event `sync.report` has `outcome=error` and the exit
code is at least 1.

### Test Mode

With `HR_SYNC_ENVIRONMENT=test`, HR-Sync writes anonymized data to the Decidalo test environment
(`DECIDALO_TEST_BASE_URL`, `DECIDALO_TEST_API_KEY`) instead of production:

- Right after loading from Personio, names, e-mail addresses and phone numbers are replaced by substitutes that only
  depend on the employee number, e.g. `Test Person-00965`, `test-00965@example.com` and `+49 000 00965`. Employee
  number, entry date, career level, office and weekly hours stay real, so strictly speaking the data is pseudonymized:
  whoever can see Personio can tell whose data it is.
- New Decidalo users never get a login.
- weclapp can't be a target, because there is no weclapp test tenant.
- A test URL that points to the production system of Decidalo (`https://import.decidalo.app`) aborts the run. The
  production mode always uses the production system and ignores the test variables. The test environment of Decidalo
  is `https://import.decidalo.dev`, which is also the default URL of the `decidalo-client`.

For a small test run, plan only one employee:

```bash
uv run hr-sync --target decidalo --max-employees 1 --dry-run
```

## Operation

HR-Sync runs every night on a server with Docker, started by [Dockhand](https://github.com/Finsys/dockhand). Every
release tag `v*` builds the image `ghcr.io/hochfrequenz/hr-sync:<version>`, also tagged `latest`, and pushes it to the
GHCR. The image contains no secrets and runs without root rights, on German time (`TZ=Europe/Berlin`): HR-Sync works
with today's date, and under UTC, 0:00 in Germany would still be the day before.

[`deploy/compose.yaml`](deploy/compose.yaml) is the template for the server. Its label `dockhand.start.at: "00:00"`
makes Dockhand start the container every day at 0:00, in the timezone of the Dockhand environment, which must be
`Europe/Berlin`. A start while a run is still going does nothing. The container stays after a run: `docker logs hr-sync`
and Dockhand show the events of the last runs, rotated after 5 × 10 MB, and its exit code is the one of the last run
(see [Usage](#usage)).

To set it up:

1. Put `deploy/compose.yaml` and the `.env` (see [`.env.example`](.env.example)) into one directory on the server,
   readable only by the operators. Fill in the ignore lists and the variables of the [report](#report).
2. Check the setup with a dry run that sends the report, marked as a dry run:

   ```bash
   docker compose run --rm hr-sync --dry-run --send-report
   ```

3. Create the container without starting it:

   ```bash
   docker compose up --no-start
   ```

   `docker compose up -d` or a deployment in Dockhand create it as well, but start a real run at once.

Instead of a copy of the file, Dockhand can also deploy `deploy/compose.yaml` from this repository (Git stack). Then
every change of the file on `main` starts a real run as soon as Dockhand syncs it, and the `.env` must come from
Dockhand, because it never goes into the repository.

To update, create a release with a new tag `vX.Y.Z` on GitHub, wait for the workflow "Docker Image", raise the version
in `compose.yaml` and recreate the container with `docker compose up --no-start`.

## Development

This project uses [uv](https://docs.astral.sh/uv/) to manage the Python interpreter, the virtual environment and the
dependencies. Create the dev environment with:

```bash
uv sync --group dev
```

The checks that run in CI:

```bash
uv run pytest
uv run ruff check src/hr_sync unittests
uv run ruff format --check .
uv run mypy --show-error-codes src/hr_sync --strict
uv run mypy --show-error-codes unittests --strict
uv run codespell --ignore-words=domain-specific-terms.txt src README.md
```

The integration tests read Personio (production) and the Decidalo test environment with the credentials from the
environment or the `.env` file in the root of the repository. They only run locally and only when selected:

```bash
uv run pytest -m integration
uv run pytest -m integration_write
uv run pytest -m integration_write --keep-test-data
```

`integration` plans with anonymized data and executes the plan as a dry run. For weclapp, which has no test
tenant, it reads production and sends the requests of the first employee of the plan with `dryRun=true`, so that
weclapp stores nothing; it needs the weclapp variables. `integration_write` additionally needs
`HR_SYNC_IT_ALLOW_WRITES=1`: it creates the first employee of the plan anonymized in the Decidalo test environment,
checks it and deletes it again, unless `--keep-test-data` keeps it.

For details on the development setup (IDE setup etc.), see the
[Hochfrequenz Python Template Repository](https://github.com/Hochfrequenz/python_template_repository).
