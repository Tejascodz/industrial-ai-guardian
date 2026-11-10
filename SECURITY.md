# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](../../security/advisories/new) for this
repository rather than in a public issue. Include steps to reproduce and the
affected file or component.

## Scope

This is a research and portfolio project that runs against a simulated plant.
The threat model, the controls in place, the tests that verify them and the
known residual risks are documented in [docs/security.md](docs/security.md).

Guardian is a read-only monitoring layer. It is not a safety system and must
never be placed in a control path.
