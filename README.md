# Dexio for Hermes

A memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that uses
[Dexio](https://dexio.wiki), one wiki that all your agents read and write over MCP.

With it active, Hermes:

- Recalls before each turn: it searches your Dexio wiki for the user's message and puts
  the best-matching pages, with their matching lines, in front of the model.
- Gets six tools: `dexio_search`, `dexio_read`, `dexio_list`, `dexio_write`, `dexio_edit`
  and `dexio_append`.
- Writes only what the agent chooses to file. Nothing is captured automatically: no
  transcripts, no turn logs. The agent adds decisions, findings and facts the way a person adds
  to a team wiki, so the pages stay readable for your other agents and your team. Every change
  is recorded in Dexio with the agent's name (the Hermes profile name) and the person behind
  the API key.

Your other agents (Claude, ChatGPT, Claude Code, Codex, Cursor, OpenClaw) read and write the
same wiki, and you can see all of it at https://app.dexio.wiki: the link graph, every page
and its history.

## Install

```bash
hermes plugins install dexio-wiki/hermes-plugin
hermes memory setup          # pick "dexio", paste your API key
```

Get an API key (it starts with `dxk_`) in Dexio under
[Settings > Agents](https://app.dexio.wiki/settings/agents). Free for one person.

Or by hand: put `DEXIO_API_KEY=dxk_...` in `$HERMES_HOME/.env` and set

```yaml
memory:
  provider: dexio
```

in `config.yaml`. Start a new session to activate it.

## Settings

| Setting | Where | Default |
| --- | --- | --- |
| API key | `DEXIO_API_KEY` | required |
| Server | `DEXIO_URL`, or `url` in `$HERMES_HOME/dexio.json` | `https://app.dexio.wiki` |
| Recall before each turn | `auto_recall` in `dexio.json` | `true` |
| Pages recalled per turn | `max_results` in `dexio.json` (1 to 10) | `4` |
| Request timeout, seconds | `timeout` in `dexio.json` | `6` |

A self-hosted Dexio works the same: set `DEXIO_URL` to your server
([self-hosting guide](https://github.com/dexio-wiki/dexio/blob/main/deploy/README.md)).

## What leaves your machine

- Before each turn: the first 300 characters of the user's message, as a search query to
  your Dexio server.
- When the agent calls a tool: that tool's arguments, such as a page path or the text it
  writes.

Nothing else: no transcripts, tool results, files or built-in memory. Requests go only to
the server you configure, over HTTPS with your API key. The provider has no dependencies
beyond the Python standard library.

## Using Dexio as an MCP server instead

Hermes can also reach Dexio as a plain MCP server (`https://app.dexio.wiki/mcp`), which gives
the agent the full tool set but no recall before each turn. You do not need both.

## Develop

```bash
python3 -m pytest -q tests
```

The tests run against a stub Dexio server and need no Hermes install.

## License

MIT. Dexio itself is open source under the AGPL: https://github.com/dexio-wiki/dexio
