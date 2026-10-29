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

Copy [`.env.example`](.env.example) to `.env` and fill in the credentials. Then run:

```bash
uv run hr-sync --help
```

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
