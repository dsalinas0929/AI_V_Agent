# Running Agent V

This file explains how to run Agent V on the server side and the client side.

## Requirements

- Python 3.11 or newer
- `uv` installed
- A checked-out copy of this project

## Install dependencies

From the project directory:

```sh
uv sync --locked
```

## Server side

The server manages sessions, authentication, and model access.

### Demo server

Use demo mode if you want to test the system without an API key:

```sh
uv run agent-v serve --demo
```

### SayGM server

To run with SayGM GPT:

```sh
export AGENT_V_PROVIDER=saygm
export AGENT_V_MODEL=gpt-5.4-mini
uv run agent-v serve --ask-api-key
```

You can also set the key directly in the environment:

```sh
export GM_API_KEY='your-saygm-key'
uv run agent-v serve
```

### Gemini server

To run with Gemini:

```sh
export AGENT_V_PROVIDER=gemini
export AGENT_V_MODEL=gemini-3.8-flash
uv run agent-v serve --ask-api-key
```

Or set the key directly:

```sh
export GEMINI_API_KEY='your-gemini-key'
uv run agent-v serve
```

## Client side

The client connects to the server and uses local workspace tools.

### Set the token

Use the token created by the server admin command:

```sh
export AGENT_V_TOKEN='paste-the-token-here'
```

### Connect to a local server

If the server is running locally:

```sh
uv run agent-v chat --workspace .
```

### Connect to a remote server

If the server is hosted elsewhere:

```sh
export AGENT_V_SERVER='https://agent.example.com'
uv run agent-v chat --workspace /path/to/project
```

## Useful commands

List sessions:

```sh
uv run agent-v sessions
```

Create a new user token on the server machine:

```sh
uv run agent-v users add alice
```

## Notes

- The server and client use different credentials.
- `AGENT_V_TOKEN` is for client authentication.
- `GM_API_KEY` and `GEMINI_API_KEY` are server-side provider keys.
- For local testing, `serve --demo` does not require a provider key.
