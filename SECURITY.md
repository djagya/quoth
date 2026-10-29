# Security policy

quoth indexes and serves private notes and chat history, so leaks and unintended
writes count as security issues.

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Don't open a public issue or pull request.

Please include the affected version, steps to reproduce, and the impact you expect.
You should get a first reply within a week.

## In scope

- Retrieved content, ledger claims or chat text reaching someone who shouldn't see it.
- Any write to the vault or to a frozen snapshot, or a mutation through `quoth mcp` or the
  sidecar's read-only routes.
- A claim recorded as the owner's words, or a review decision applied, without the owner's
  verified message.
- Prompt-injection paths where retrieved evidence is treated as an instruction.
- Secrets or private data in published artifacts (wheel, release assets, logs).

## Out of scope

- Access by a local process that can already connect to the sidecar's Unix socket. The
  socket's file permissions are the sidecar's trust boundary by design; see
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#trust-boundaries).

## Supported versions

Only the latest release gets fixes.
