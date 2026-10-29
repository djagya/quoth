# quoth (Hermes memory provider)

A stdlib-only Hermes memory provider that talks to `quoth sidecar` over a Unix socket.
Install it from the same quoth release as the sidecar, so the two always match:

```sh
quoth hermes-plugin install --hermes-home "$HERMES_HOME"     # --force to upgrade in place
```

Then set `memory.provider: quoth` in the Hermes config and run `quoth sidecar` from the same
install.

## What it provides

- `system_prompt_block`: the compiled core memory plus short tool guidance;
- `prefetch`: recall for each turn (accepted claims, vault evidence, the owner's earlier
  words), only in primary sessions on the platforms in `auto_recall_platforms`;
- tools: `propose_claim`, `review_claims`, `evidence_bundle`, `episode_search`;
- `on_memory_write`: Hermes built-in MEMORY/USER writes become pending `imported` claims;
- the `/quoth` chat command: read-only views of the core, the review queue, single claims
  and a recall dry run.

## Configuration

Socket: `QUOTH_SOCKET`, else `{"socket": …}` in `$HERMES_HOME/quoth.json`, else
`$QUOTH_HOME/quoth.sock` (`QUOTH_HOME` defaults to `$XDG_DATA_HOME/quoth`, else
`~/.local/share/quoth`). `quoth.json` may also set `auto_recall_platforms` (default
`["telegram"]`). While the socket is missing, the provider reports itself unavailable.

The socket's file permissions are the only access control: give the gateway and the sidecar
a shared group and keep the socket at mode 0660.

## Skills

The plugin carries two skills, installed with it under `plugins/quoth/skills/`:

- `memory-review`: the review loop over the pending queue, run in the owner's chat;
- `memory-caller`: when and how to call the read-only evidence tools.

List that directory once in the Hermes config so the skills are indexed like local ones
(they stay read-only, and a local skill of the same name wins):

```yaml
skills:
  external_dirs:
    - <HERMES_HOME>/plugins/quoth/skills
```

See `docs/HERMES.md` in the quoth repository for the sidecar API and the full wiring.
