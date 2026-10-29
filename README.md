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
| `--only EMPLOYEE_NUMBER ...` | restrict the plan to these employee numbers; all employees are loaded anyway, to find data problems |
| `--target weclapp\|decidalo ...` | the target systems (default: all); the others are neither loaded nor written |
| `--env-file PATH` | read the configuration from this file instead of `.env` in the current directory |
| `--log-format pretty\|json` | the format of the events (default: `HR_SYNC_LOG_FORMAT`, otherwise `pretty`) |

| Exit code | Meaning |
|---|---|
| 0 | success, also with an empty plan or with notices |
| 1 | at least one operation failed or was skipped |
| 2 | aborted, e.g. because of the configuration or an error while loading |

> [!NOTE]
> weclapp can't be a target yet, and the test mode (`HR_SYNC_ENVIRONMENT=test`) is not available yet. Both abort the
> run with exit code 2, so for now, every run needs `--target decidalo`.

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

For details on the development setup (IDE setup etc.), see the
[Hochfrequenz Python Template Repository](https://github.com/Hochfrequenz/python_template_repository).
