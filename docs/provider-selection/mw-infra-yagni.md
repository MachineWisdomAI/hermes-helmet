# Infra YAGNI: operational provider selection

## Verdict
PASS

## Classification
operator-workflow: adapt Hermes Agent's existing ordered fallback chain. Helmet
provides named routes, a manual override and a transport interface; it does not
implement request retries, quota tracking or a second provider router.

## Anti-pattern hits
None. Credentials remain in the existing worker account. The Captain owns the
non-secret selection record; the worker owns its ordinary Hermes profiles.
No new service, account, shared home, mount or network endpoint is introduced.

## Complexity score
Score: 0. No new cross-cutting infrastructure mechanism. Ordinary replacement
of a small single-owner configuration file follows the existing platform model.

## Buy/adapt options
| Option | Covers | Workflow adaptation | Residual |
| --- | --- | --- | --- |
| Hermes `model` and `fallback` commands | Primary and ordered backup routes | Configure each worker profile directly | Provisioning must preserve the operator's choice |
| Hermes config plus Helmet transport | Native fallback and named manual selection | One Captain command applies the selection through the existing installer | Small CLI and persistent selection record |

## Build justification
Adapt the second option. Portable CLI, validation and config projection belong
in `hermes-helmet`; each deployment owns authentication, storage location,
effective-policy projection, drain checks and installer invocation. No private
company configuration belongs in the public implementation.

## Falsifier
The design fails if reinstall restores a different primary, a manual pin still
falls back, a configured chain silently adds another provider, or switching
interrupts a running task. Native quota recovery requires separate runtime
acceptance; current upstream documentation alone does not establish it for an
installed worker.

## Operator decision
Automatic fallback plus manual switching, requested on 2026-10-15.
