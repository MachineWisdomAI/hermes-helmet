# Company deployment overlay

This private overlay is how a company keeps deployment configuration in its
own repository. Depend on the published Hermes Helmet image by digest. Do not
fork the core source, add it as a submodule, or mount a replacement control
loop over `/opt/hermes-helmet`.

The examples below use ExampleCo names, paths, and the public ExampleCo policy
fixture. Replace those values. They are illustrations, not a hosted service.

## What each side owns

The public image owns the worker runtime: Hermes Agent, the Helmet control
loop under `/opt/hermes-helmet`, the entrypoint
`/opt/hermes-helmet/runtime-entrypoint.sh`, the `/usr/local/bin/gh` wrapper,
non-root UID/GID `10000`, and the `/opt/data` state layout.

The company owns the authority policy, repository allowlist, Captain and
worker GitHub identities, the worker token and provider credentials, named
volume, optional extra files or tools, and optional skill packs. Optional
OpenViking, FAVA Trails, and extra model lanes stay off until you enable them;
see [openviking.md](openviking.md), [fava-trails.md](fava-trails.md), and
[model-lanes.md](model-lanes.md).

## Start with mounts

A company deployment repository can look like this:

```text
exampleco-helmet/
  compose.yaml          # copyable example below; paths are relative to this file
  policy.json           # secret-free version-2 authority document
  .env                  # HERMES_GITHUB_TOKEN; owner-only; not in Git
  .gitignore            # ignore .env and credential files
```

Copy `config/policy.example.json` from Hermes Helmet, then replace
`example-captain`, `example-agent`, `example-org/demo-repo`, and the worktree
with your accounts and checkout. Keep Captain and worker as two GitHub
accounts. Leave `integrations.openviking`, `integrations.fava_trails`, and
`integrations.signal` false until those guides say otherwise.

The policy is not a credential store. Put the worker PAT in `.env` (mode
`0600`, your host user). Provider keys stay in owner-only files inside the
worker volume after login, never in Git and never in image layers.

The mounted policy must be readable by container UID `10000`. A file mode
`0600` owned by an unrelated host UID is invisible inside the container and
startup cannot copy it. Mode `0644`, or ownership `10000:10000`, is enough for
this secret-free file.

Pin the current public preview with:

```text
ghcr.io/machinewisdomai/hermes-helmet/runtime@sha256:f6e2375441cec50cac370941f4bfa53e7b2af0b8bff45ee177da6e49b760caea
```

See [runtime-image.md](runtime-image.md) for provenance and how to change that
digest.

## ExampleCo Compose

This file is a standalone Compose document for the company repository. It does
not include a `build:` section. Paths are relative to the file.

```yaml
name: exampleco-hermes-helmet

services:
  hermes:
    image: ghcr.io/machinewisdomai/hermes-helmet/runtime@sha256:f6e2375441cec50cac370941f4bfa53e7b2af0b8bff45ee177da6e49b760caea
    container_name: exampleco-hermes-helmet
    command: ["gateway", "run"]
    restart: unless-stopped
    user: "10000:10000"
    volumes:
      - exampleco_hermes_state:/opt/data
      - ./policy.json:/opt/hermes-helmet/config/policy.mounted.json:ro
    tmpfs:
      - /run/hermes-helmet:uid=10000,gid=10000,mode=0700
    environment:
      HERMES_UID: "10000"
      HERMES_GID: "10000"
      HERMES_HOME: /opt/data
      HOME: /opt/data
      HERMES_GATEWAY_NO_SUPERVISE: "0"
      HERMES_DASHBOARD: "0"
      HERMES_HELMET_POLICY_SOURCE: /opt/hermes-helmet/config/policy.mounted.json
      GH_TOKEN: ${HERMES_GITHUB_TOKEN:?set HERMES_GITHUB_TOKEN in .env}
      GITHUB_TOKEN: ${HERMES_GITHUB_TOKEN:?set HERMES_GITHUB_TOKEN in .env}
    ports:
      - "127.0.0.1:9119:9119"
    networks:
      - exampleco_hermes_helmet_private

volumes:
  exampleco_hermes_state:
    name: exampleco_hermes_helmet_state

networks:
  exampleco_hermes_helmet_private:
    name: exampleco_hermes_helmet_private
    driver: bridge
```

`.env` next to that file:

```sh
HERMES_GITHUB_TOKEN=ghp_example_synthetic_token_not_a_secret
```

Start it with:

```sh
docker pull ghcr.io/machinewisdomai/hermes-helmet/runtime@sha256:f6e2375441cec50cac370941f4bfa53e7b2af0b8bff45ee177da6e49b760caea
docker compose --env-file .env -f compose.yaml up -d --no-build
```

Compose injects the token only so the entrypoint can write
`/run/hermes-helmet/github-token` on the tmpfs (UID/GID `10000`, mode `0700`).
The entrypoint then unsets `GH_TOKEN` and `GITHUB_TOKEN` before upstream
startup. Supervised processes do not inherit the PAT; `/usr/local/bin/gh`
reads the file.

When `HERMES_HELMET_POLICY_SOURCE` points at the read-only mount
`/opt/hermes-helmet/config/policy.mounted.json`, startup copies it to
`/opt/data/github-issue-poller/policy.json` on the named volume. Install the
poller and run intake against that copied path, as in the
[quickstart](quickstart.md).

Use a company-specific volume name. The public `deploy/compose.yaml` uses
`hermes_helmet_state`; sharing that name across deployments mixes state.

After the container is up, clone allowlisted checkouts inside `/opt/data/repos`
with the worker identity, create the policy `assignee` profile, and complete
that profile's provider login. Those steps are the same as the public
quickstart; they run in this container, not on the Captain host.

## Optional thin image

Most companies only need the mounts above. Add a Dockerfile when you must bake
in extra files or tools. Inherit the pinned digest, keep the entrypoint, keep
UID/GID `10000`, and leave `/opt/hermes-helmet` and `/usr/local/bin/gh` alone.

```dockerfile
FROM ghcr.io/machinewisdomai/hermes-helmet/runtime@sha256:f6e2375441cec50cac370941f4bfa53e7b2af0b8bff45ee177da6e49b760caea

USER root
COPY company-files/ /opt/exampleco/files/
RUN chown -R root:root /opt/exampleco/files \
    && chmod -R a+rX /opt/exampleco/files
USER 10000:10000
```

Point Compose `image:` at the tag you build from that file, or keep using the
public digest when you have nothing to add. Do not replace `ENTRYPOINT`, do not
run the long-lived process as root, and do not copy Helmet source over the
image tree.

## Worker instructions and approved skills

One version-2 policy is the authority document. From it you can render the
crew contract that seeds the worker profile:

```python
from pathlib import Path
from hermes_helmet.authority import load_authority, render_crew_contract

policy = load_authority(Path("policy.json"))
print(render_crew_contract(policy))
```

Kanban work runs as `policy.assignee` (ExampleCo: `builder`). Create that
profile in the worker container and point it at `inference_provider` /
`inference_model`. Captain skills (`setup-helmet`, `helmet-issue`,
`helmet-epic`) install on the Captain host only.

Approved executor skills enter through the company pack importer. Point
`skills.company_pack.source` at an absolute path the container can read, list
exact skill directory names in `allowlist`, and let startup call
`helmet import-company-skills`. Import writes Hermes-owned state under
`/opt/data`; it does not install Captain skills and cannot grant review or
merge authority. See [company-skills.md](company-skills.md).

Public helpers you can call instead of forking them:

```python
from hermes_helmet.authority import load_authority, render_crew_contract
from hermes_helmet.preflight import StaticIdentityProbe, assert_worker_ready

policy = load_authority(Path("policy.json"))
assert_worker_ready(policy, StaticIdentityProbe(observed_login))
```

## Change the image, keep the work

Follow [runtime-image.md](runtime-image.md#change-the-pin-or-roll-back). Keep
`exampleco_hermes_helmet_state` so the poller ledger and checkouts survive.
Pause dispatch and finish or block active issues before you change repository
slugs or worktree paths. There is no extra platform-management command; intake
is the dispatch label plus the poller, and rollback is the previous digest
against the same volume.
