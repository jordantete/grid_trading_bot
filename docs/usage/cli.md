# CLI Reference

The bot provides a command-line interface built with [Click](https://click.palletsprojects.com/).

## Installation

Once installed, the `grid_trading_bot` command is available:

```bash
# Via uv (recommended)
uv run grid_trading_bot <command>

# Or directly if installed in your environment
grid_trading_bot <command>

# Or via python -m
uv run python -m grid_trading_bot <command>
```

## Commands

### `run`

Run the trading bot with one or more configuration files.

```bash
uv run grid_trading_bot run --config config/config.json
```

**Options:**

| Option | Type | Required | Description |
|--------|------|----------|-------------|
| `--config` | string | Yes | Path to configuration file. Can be specified multiple times for concurrent execution. |
| `--save_performance_results` | string | No | Path to save simulation results as JSON (e.g., `results.json`). |
| `--no-plot` | flag | No | Disable interactive Plotly charts at the end of simulation. |
| `--profile` | flag | No | Enable profiling for performance analysis. |

### `--version`

Display the installed version:

```bash
uv run grid_trading_bot --version
```

## Usage Examples

### Basic backtest

```bash
uv run grid_trading_bot run --config config/config.json
```

### Multiple configurations

Run several strategies concurrently. Each config runs as an independent bot instance via `asyncio.gather`:

```bash
uv run grid_trading_bot run \
  --config config/config1.json \
  --config config/config2.json \
  --config config/config3.json
```

### Save performance results

Export metrics to a JSON file for later analysis:

```bash
uv run grid_trading_bot run --config config/config.json --save_performance_results results.json
```

### Headless mode (no plots)

Useful for CI pipelines or batch runs:

```bash
uv run grid_trading_bot run --config config/config.json --no-plot
```

### Combined options

```bash
uv run grid_trading_bot run \
  --config config/config1.json \
  --config config/config2.json \
  --save_performance_results combined_results.json \
  --no-plot
```

### With profiling

Generate a `.prof` file for performance analysis:

```bash
uv run grid_trading_bot run --config config/config.json --profile
```

### `resume`

Clear the halt recorded for a config, so the next start is a fresh grid.

```bash
uv run grid_trading_bot resume --config config/config.live.json
```

| Option | Type | Required | Description |
|--------|------|----------|-------------|
| `--config` | string | Yes | Path to the configuration file of the halted bot. |

The command has three outcomes:

- **Halt cleared**: the state database (and its `-wal` and `-shm` files) is moved aside as `<name>.halted-<UTC timestamp>` and the halt marker is deleted.
- **Nothing to resume**: no halt is recorded, nothing changes, and the command says the next start resumes the saved state.
- **Refused**: a bot holds the config's lock, so the command exits with code 1.

See [Going Live](going-live.md#4-stopping) for what causes a halt.

## Runtime Commands

In **live** and **paper trading** modes, the bot starts a command listener (BotController) that accepts commands while the bot is running. The listener runs only in an interactive terminal: in a detached container or under a service manager it does not start.

The listener prints no prompt. When it starts, it logs:

```
Command listener started. Commands: orders, balance, stop, quit.
```

### Command Reference

| Command | Description |
|---------|-------------|
| `stop` | Halts the bot: open orders are cancelled, the position is kept, and the process exits with code 0. |
| `quit` | Same as `stop`. |
| `orders` | Displays all active orders in a formatted table. |
| `balance` | Displays current fiat and crypto balances. |

Commands are case-insensitive. Invalid commands are logged as warnings.

After `stop` or `quit`, no relaunch trades until [`resume`](#resume) is run.

### Examples

Type a command and press Enter.

Halt the bot:

```
stop
```

View active orders:

```
orders
```

The `orders` command outputs a table with the following columns:

| Column | Description |
|--------|-------------|
| Order Side | Buy or Sell |
| Type | Order type (limit, market) |
| Status | Current order status |
| Price | Order price |
| Quantity | Order quantity |
| Timestamp | When the order was placed |
| Grid Level | Associated grid level index |
| Slippage | Price slippage from expected fill |

!!! note
    The runtime command listener is **not available** in backtest mode, since backtests run to completion without user interaction.
