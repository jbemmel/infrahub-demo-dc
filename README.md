# Infrahub demo

<!-- rumdl-disable -->
![Infrahub Logo](https://assets-global.website-files.com/657aff4a26dd8afbab24944b/657b0e0678f7fd35ce130776_Logo%20INFRAHUB.svg)
<!-- rumdl-enable -->

[Infrahub](https://github.com/opsmill/infrahub) by [OpsMill](https://opsmill.com) acts as a central hub to manage the data, templates and playbooks that powers your infrastructure. At its heart, Infrahub is built on 3 fundamental pillars:

- **A Flexible Schema**: A model of the infrastructure and the relation between the objects in the model, that's easily extensible.
- **Version Control**: Natively integrated into the graph database which opens up some new capabilities like branching, diffing, and merging data directly in the database.
- **Unified Storage**: By combining a graph database and git, Infrahub stores data and code needed to manage the infrastructure.

> **Note**
> This demo repository is partially authored by the OpsMill community member [tomek](https://www.linkedin.com/in/tomekzajac/) from this example: <https://github.com/t0m3kz/infrahub-demo>

## Infrahub demo

This repository is demoing the key Infrahub features for an example data center with VxLAN/EVPN and firewalls.

## Running the demo

Documentation for loading and using this demo is available on the Infrahub docs site [docs.infrahub.app/demo-dc/](https://docs.infrahub.app/demo-dc)

Copy `.env.example` to `.env` if you have not already done so, and configure your
Infrahub address and API token. Load that file explicitly when bootstrapping:

```bash
uv run --env-file .env invoke bootstrap
```

`uv run invoke bootstrap` does not automatically load `.env`. Without an exported
`INFRAHUB_API_TOKEN`, schema loading fails with `Authentication is required`.
Use `uv run --env-file .env` for other demo commands that need these settings too.

This demo requires Infrahub 1.11 or later for generator `watch` declarations.
An existing, Git-ignored `docker-compose.yml` takes precedence over the downloaded
stack and is not refreshed by pulling this repository or resetting the demo.
If repository sync reports `error-import` with `generator_definitions.*.watch`
validation errors, refresh the local stack definition before initializing again:

```bash
curl -fsSL https://infrahub.opsmill.io/1.11.5 -o docker-compose.yml
uv run --env-file .env invoke init
```

`invoke init` destroys the demo's existing containers and volumes, including its data.

If bootstrap skips optional event actions because the `create_dc` generator is not
available yet, wait for the repository to finish syncing in Infrahub, then retry:

```bash
uv run --env-file .env infrahubctl object load objects/events/ --branch main
```

## Service Catalog

This repository includes an optional Streamlit-based Service Catalog that provides a user-friendly web interface for viewing and creating data center infrastructure in Infrahub.

### Features

- View lists of Data Centers and Colocation Centers with branch selection
- Create new Data Centers through a form-based interface
- Automatic branch creation and Proposed Change generation
- Workflow automation for infrastructure provisioning

### Quick Start

To start Infrahub with the Service Catalog enabled, set `INFRAHUB_SERVICE_CATALOG=true` in `.env`, then run:

```bash
uv run --env-file .env invoke start
```

The Service Catalog will be accessible at `http://localhost:8501`. It runs the published image `registry.opsmill.io/opsmill/infrahub-demo-dc-streamlit`, so there is nothing to build. To build it from this checkout instead, run `uv run invoke start --rebuild`.

To start Infrahub without the Service Catalog, leave `INFRAHUB_SERVICE_CATALOG` unset or set it to `false`.

### Documentation

For detailed setup instructions, configuration options, and usage guide, see the [demo-dc docs](https://docs.infrahub.app/demo-dc).

## Testing

```bash
# Unit and specification tests: seconds, no containers
uv run invoke test-unit

# The integration tier every pull request runs
uv run invoke test-integration --tier=core

# Everything, including the extended workflows
uv run invoke test-integration --tier=full
```

The integration suite starts its own throwaway Infrahub deployment with
[infrahub-testcontainers](https://pypi.org/project/infrahub-testcontainers/), bootstraps it from this
repository and drives complete workflows against it: building a data center from a design, generating
device configurations, reviewing and merging a proposed change, adding a POP and a network segment,
day-two operations, and conflict detection. It needs Docker; it does not use `invoke start`.

The whole suite shares one deployment, so modules run in file-name order and build on what earlier
ones merged. They are split into two tiers:

- **`core`** runs on every pull request.
- **`extended`** adds the second vendor data center, the POP and segment services, day-two operations
  and the conflict workflow. It runs on a pull request that bumps the Infrahub version, and on a
  manual run of the CI workflow with its `tier` input set to `full`.

On a pull request that bumps the Infrahub version, a failure in the extended tier blocks the merge.
On a manual run it does not, because it does not pass reliably: Infrahub intermittently fails to
resolve a member of its own internal generator group, which fails whichever generator is running at
the time. Dispatch it deliberately when changing a generator or a transform, and read a
failure there against that known fault before assuming the demo is broken. To test a specific
Infrahub version, set `INFRAHUB_TESTING_IMAGE_VER`.

See the [developer guide](https://docs.infrahub.app/demo-dc/developer-guide) for details, and
[tests/AGENTS.md](tests/AGENTS.md) for the conventions the suite follows.

## License

This project is licensed under the MIT License. See [LICENSE.txt](LICENSE.txt) for details.
