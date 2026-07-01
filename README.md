# IP Access Monitor

Monitor IP reachability worldwide using the [check-host.net](https://check-host.net) API. Get instant **Telegram alerts** when your IPs go down or become Iran-only accessible. Python + systemd, with priority nodes for Iran and Germany.

## Features

- **Multi-node ping checks** via check-host.net's global network
- **Iran + Germany priority** — all nodes from these countries are always included
- **Multiple Telegram recipients** — send alerts to several chat IDs
- **Acknowledge button** — tap to snooze an IP for 30 min directly from Telegram
- **Bot commands** — `/snooze`, `/unsnooze`, `/status`, `/list`, `/help`
- **Three alert conditions**:
  - 🔴 **DOWN** — no location can ping the IP
  - 🟡 **Iran-only** — only Iranian nodes can reach the IP
  - 🟠 **Degraded** — partial reachability below the threshold
- ✅ **Recovery alerts** — automatic notification when an IP comes back online
- **Rate-limit friendly** — exponential backoff, smart polling, configurable intervals
- **systemd service** — runs 24/7 with auto-restart
- **Interactive installer** — guided setup in under a minute

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/mahdipatriot/IP-access-monitor.git
cd IP-access-monitor

# 2. Run the installer
chmod +x install.sh
sudo ./install.sh

# 3. Add your IPs
nano ips.txt
```

The installer will:
1. Prompt for your Telegram bot token and chat ID(s) (with test messages)
2. Configure monitoring parameters
3. Install Python dependencies
4. Create and start a systemd service

## Prerequisites

- **Python 3.8+**
- **pip**
- **systemd** (Linux — for 24/7 service)
- A **Telegram Bot** — create one via [@BotFather](https://t.me/BotFather) and get the bot token
- Your **Telegram Chat ID** — get it from [@userinfobot](https://t.me/userinfobot)

## Manual Setup (without installer)

```bash
# 1. Copy and edit the environment file
cp .env.example .env
nano .env  # fill in your Telegram token and chat ID(s)

# 2. Add IPs to monitor
nano ips.txt

# 3. Install dependencies
pip3 install -r requirements.txt
# On newer Debian/Ubuntu (PEP 668), use:
# pip3 install --break-system-packages -r requirements.txt
# Or use system packages:
# apt install python3-requests python3-dotenv

# 4. Run
python3 monitor.py

# Or use the launcher script
chmod +x run.sh
./run.sh
```

## Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Comma-separated chat IDs (e.g. `id1,id2,id3`) |
| `CHECK_INTERVAL` | `120` | Seconds between full check cycles |
| `RESULT_WAIT` | `5` | Seconds to wait before polling results |
| `NODE_CACHE_TTL` | `86400` | Node list cache TTL in seconds (24h) |
| `MAX_NODES` | `20` | Max non-priority global nodes to use |
| `ALERT_THRESHOLD` | `0.7` | Fraction of nodes that must be OK (0.7 = 70%) |
| `SNOOZE_MINUTES` | `30` | Default snooze duration for ack button |
| `PRIORITY_COUNTRIES` | `ir,de` | Countries whose nodes are always included |

### IP List (`ips.txt`)

One IP or hostname per line. Lines starting with `#` are ignored:

```
# My servers
1.2.3.4
5.6.7.8
example.com
```

## How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  ips.txt    │────▶│  monitor.py      │────▶│  check-host.net │
│  (IP list)  │     │  (main loop)     │     │  API            │
└─────────────┘     │                  │     └────────┬────────┘
                    │  ┌────────────┐  │              │
                    │  │ nodes.py   │◀─┼──────────────┘
                    │  │ (select)   │  │  node list + ping results
                    │  └────────────┘  │
                    │  ┌────────────┐  │
                    │  │ evaluator  │  │  → DOWN / IRAN_ONLY / DEGRADED / OK
                    │  └────────────┘  │
                    │  ┌────────────┐  │
                    │  │ snooze.py  │  │  → skip alert if snoozed
                    │  └────────────┘  │
                    │  ┌────────────┐  │
                    │  │ telegram   │──┼──▶  Telegram alerts (with ack button)
                    │  └────────────┘  │
                    │  ┌────────────┐  │
                    │  │ bot.py     │◀─┼──▶  Telegram commands (/snooze, /status...)
                    │  └────────────┘  │
                    └──────────────────┘
```

### Node Selection

1. Fetch all nodes from `check-host.net/nodes/hosts`
2. **Always include ALL nodes from priority countries** (Iran `ir`, Germany `de`)
3. Add up to `MAX_NODES` other global nodes for broad coverage
4. Cache the node list locally (refreshes every 24h)

### Alert Conditions

| Condition | Criteria | Alert |
|-----------|----------|-------|
| 🔴 **DOWN** | 0 nodes have OK ping | Yes (with ack button) |
| 🟡 **Iran-only** | Only Iranian nodes OK, all others (incl. Germany) fail | Yes (with ack button) |
| 🟠 **Degraded** | Some nodes OK but below threshold (<70%) | Yes (with ack button) |
| ✅ **OK** | ≥70% of nodes have OK ping | No (recovery alert if was non-OK) |

### Snooze System

Alerts fire **every cycle** until you acknowledge them. No auto-snooze — you stay notified.

**Via Telegram inline button:**
- Every alert includes a "🔇 Acknowledge (30 min)" button
- Tap it to snooze that IP for 30 minutes
- After 30 min, if the condition persists, alerts resume automatically

**Via Telegram bot commands:**

| Command | Description |
|---------|-------------|
| `/snooze <IP> [minutes]` | Snooze alerts for an IP (default 60 min) |
| `/unsnooze <IP>` | Remove snooze for an IP immediately |
| `/status` | Show all monitored IPs and their last known condition |
| `/list` | Show all currently snoozed IPs with remaining time |
| `/help` | Show available commands |

**Via CLI (on the server):**

```bash
python3 monitor.py --snooze 1.2.3.4 60    # snooze for 60 min
python3 monitor.py --unsnooze 1.2.3.4     # remove snooze
python3 monitor.py --status               # show current status
```

### Recovery Alerts

When an IP transitions from non-OK → OK:
- Sends a green ✅ recovery message to all chat IDs
- Clears all snoozes for that IP

### Rate Limiting

- Exponential backoff on API errors (429, 5xx)
- Max 3 retries per request
- Results polled with configurable wait (default: 5s between polls, max 6 polls)
- All IPs checked per cycle, then sleep before next cycle

## Service Management

```bash
# Status
sudo systemctl status ip-access-monitor

# Stop / Start / Restart
sudo systemctl stop ip-access-monitor
sudo systemctl start ip-access-monitor
sudo systemctl restart ip-access-monitor

# View logs (live)
journalctl -u ip-access-monitor -f

# View last 100 lines
journalctl -u ip-access-monitor -n 100
```

## Single Check (no loop)

```bash
python3 monitor.py --once
```

## Project Structure

```
IP-access-monitor/
├── README.md              # This file
├── LICENSE                # MIT license
├── install.sh             # Interactive installer (prompts, deps, systemd)
├── run.sh                 # Bash launcher (manual runs)
├── monitor.py             # Main Python script
├── requirements.txt       # Python dependencies (requests, python-dotenv)
├── .env.example           # Environment variable template
├── ips.txt                # IPs to monitor (one per line)
├── .gitignore
└── src/
    ├── __init__.py
    ├── checkhost_api.py   # check-host.net API client
    ├── nodes.py           # Node selection + caching (Iran + Germany priority)
    ├── telegram.py        # Telegram alerts (multi-chat + inline ack buttons)
    ├── evaluator.py       # Result evaluation logic
    ├── snooze.py          # Snooze state management (persistent)
    └── bot.py             # Telegram bot command + callback handler
```

## License

MIT — feel free to use, modify, and distribute.

## Contributing

Pull requests welcome! Please open an issue first to discuss what you'd like to change.
