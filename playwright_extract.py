#!/usr/bin/env python3
"""
Slack Token Extractor - Playwright Edition

Extract Slack XOXC and XOXD tokens using Playwright automation.
Each MCP server gets its own isolated browser profile and credentials file
so tokens are independent and don't expire together.

Servers are defined in ~/.config/slack-token-extractor/servers.json
(see servers.example.json). Keys are server names; each entry takes
mcp_name, workspace, creds_file, and optional team, user, description.

Requirements:
    pip install playwright
    playwright install chromium

Usage:
    # Refresh a specific server's tokens
    python playwright_extract.py --server slack

    # Refresh all servers sequentially (opens browser once per server)
    python playwright_extract.py --server all

    # Headless mode (requires existing session in that profile)
    python playwright_extract.py --server slack --headless

    # Show current token status across all servers
    python playwright_extract.py --status
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("Error: Playwright not installed.")
    print("Install with: pip install playwright && playwright install chromium")
    sys.exit(1)


# ── Server definitions ────────────────────────────────────────────────

BASE_PROFILE_DIR = Path.home() / ".slack-token-extractor"
MCP_CONFIG = Path.home() / ".claude.json"
CREDS_DIR = Path.home() / ".config" / "slack-cli"

SERVERS_CONFIG = Path.home() / ".config" / "slack-token-extractor" / "servers.json"


def load_servers(path: Path = SERVERS_CONFIG) -> dict:
    """Load per-server definitions from the local JSON config.

    Workspace URLs and team names are user-specific, so they live outside the
    repo. Each entry gets a derived isolated browser profile directory.
    """
    if not path.exists():
        sys.exit(f"Error: no server config at {path} (copy servers.example.json there)")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"Error: {path} is not valid JSON: {e}")
    servers = {}
    for name, entry in raw.items():
        missing = {"mcp_name", "workspace", "creds_file"} - entry.keys()
        if missing:
            sys.exit(f"Error: server '{name}' in {path} is missing {sorted(missing)}")
        servers[name] = {
            "mcp_name": entry["mcp_name"],
            "profile_dir": BASE_PROFILE_DIR / f"profile-{name}",
            "creds_file": CREDS_DIR / entry["creds_file"],
            "workspace": entry["workspace"],
            "team": entry.get("team", name),
            "user": entry.get("user", ""),
            "description": entry.get("description", ""),
        }
    return servers


SERVERS = load_servers()


def extract_tokens(
    workspace_url: str = "https://app.slack.com/client/",
    headless: bool = False,
    profile_dir: Path = BASE_PROFILE_DIR / "profile-slack",
) -> dict | None:
    """
    Extract XOXC token and XOXD cookie from Slack.

    Profile dir is wiped and recreated on interactive runs to guarantee a fresh
    browser session → independent token pair per server.
    Headless runs preserve the existing profile to reuse saved sessions.

    Returns:
        Dictionary with 'xoxc_token', 'xoxd_token', and 'team_id' or None on failure
    """
    import shutil
    if not headless and profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        print(f"Launching browser (headless={headless})...")
        print(f"Profile directory: {profile_dir}")

        browser = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1280, "height": 800},
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        # Block the "open in Slack app" redirect
        page.add_init_script("""
            window.addEventListener('beforeunload', (e) => { e.preventDefault(); });
            if (navigator.registerProtocolHandler) {
                navigator.registerProtocolHandler = () => {};
            }
        """)

        # Navigate to the web client
        web_url = workspace_url.rstrip("/")
        if "/client" not in web_url:
            web_url = web_url + "/"
        print(f"Navigating to {web_url}...")
        page.goto(web_url)

        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeout:
            pass

        current_url = page.url
        # Only /client/ means we're actually inside the workspace
        needs_login = "/client/" not in current_url

        if needs_login:
            if headless:
                print("\nError: Not logged in and running in headless mode.")
                print("Run without --headless first to log in.")
                browser.close()
                return None

            print("\n" + "=" * 60)
            print("Log in and get to the workspace in the browser.")
            print("Click through any popups ('Use in browser', etc).")
            print("Script will detect when tokens appear in localStorage.")
            print("=" * 60)

            import time

            # Poll localStorage for xoxc token (works regardless of URL)
            for i in range(360):  # 6 minutes max
                time.sleep(1)
                if i % 5 == 0:
                    print(f"  [{i}s] waiting... URL: {page.url}")
                try:
                    token = page.evaluate("""() => {
                        try {
                            const config = JSON.parse(localStorage.localConfig_v2 || '{}');
                            for (const [tid, data] of Object.entries(config.teams || {})) {
                                if (data.token && data.token.startsWith('xoxc-')) {
                                    return data.token.substring(0, 20);
                                }
                            }
                        } catch {}
                        return null;
                    }""")
                    if token:
                        print(f"  Token detected in localStorage: {token}...")
                        break
                except Exception:
                    pass  # Page might be navigating
            else:
                print(f"Timed out waiting for login. Last URL: {page.url}")
                browser.close()
                return None

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass

        # Extract team ID
        current_path = page.url
        print(f"Extracting team ID from: {current_path}")
        team_id_match = re.search(r"/client/([A-Z0-9]+)", current_path)

        if not team_id_match:
            team_id = page.evaluate("""() => {
                try {
                    const config = JSON.parse(localStorage.localConfig_v2 || '{}');
                    const teams = Object.keys(config.teams || {});
                    return teams[0] || null;
                } catch { return null; }
            }""")
        else:
            team_id = team_id_match.group(1)

        if not team_id:
            print("\nError: Could not determine team ID.")
            browser.close()
            return None

        print(f"Found team ID: {team_id}")

        # Extract XOXC token from localStorage
        print("Extracting XOXC token from localStorage...")
        xoxc_token = page.evaluate("""(teamId) => {
            try {
                const config = JSON.parse(localStorage.localConfig_v2 || '{}');
                if (config.teams && config.teams[teamId]) {
                    return config.teams[teamId].token;
                }
                for (const [tid, data] of Object.entries(config.teams || {})) {
                    if (data.token && data.token.startsWith('xoxc-')) {
                        return data.token;
                    }
                }
            } catch {}
            return null;
        }""", team_id)

        if not xoxc_token:
            xoxc_token = page.evaluate("""() => {
                const match = document.body.innerHTML.match(/"token":"(xoxc-[^"]+)"/);
                return match ? match[1] : null;
            }""")

        if not xoxc_token:
            print("\nError: Could not find XOXC token.")
            browser.close()
            return None

        print(f"Found XOXC token: {xoxc_token[:25]}...{xoxc_token[-10:]}")

        # Extract XOXD cookie
        print("Extracting XOXD token from cookies...")
        cookies = browser.cookies()
        xoxd_token = None

        for cookie in cookies:
            if cookie["name"] == "d" and "slack.com" in cookie["domain"]:
                xoxd_token = cookie["value"]
                break

        if not xoxd_token:
            print("\nError: Could not find 'd' cookie (XOXD token).")
            browser.close()
            return None

        print(f"Found XOXD token: {xoxd_token[:25]}...{xoxd_token[-10:]}")

        browser.close()

        return {
            "xoxc_token": xoxc_token,
            "xoxd_token": xoxd_token,
            "team_id": team_id,
        }


# ── Per-server token storage ──────────────────────────────────────────

def store_tokens(server_name: str, tokens: dict) -> list[str]:
    """Write tokens to the target server's MCP env and credentials file.

    Only touches the specified server — never overwrites other servers' tokens.

    Returns list of updated stores for display.
    """
    server = SERVERS[server_name]
    xoxc = tokens["xoxc_token"]
    xoxd = tokens["xoxd_token"]
    team_id = tokens.get("team_id", "")
    updated: list[str] = []

    # 1. Update this server's env in ~/.claude.json
    mcp_name = server["mcp_name"]
    if MCP_CONFIG.exists():
        try:
            cfg = json.loads(MCP_CONFIG.read_text())
            srv = cfg.get("mcpServers", {}).get(mcp_name)
            if srv is not None:
                env = srv.setdefault("env", {})
                env["SLACK_MCP_XOXC_TOKEN"] = xoxc
                env["SLACK_MCP_XOXD_TOKEN"] = xoxd
                MCP_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
                MCP_CONFIG.chmod(0o600)
                updated.append(f"MCP:{mcp_name}")
            else:
                print(f"  Warning: MCP server '{mcp_name}' not found in {MCP_CONFIG}", file=sys.stderr)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to update {MCP_CONFIG}: {e}", file=sys.stderr)

    # 2. Update this server's credentials file
    creds_file = server["creds_file"]
    try:
        creds_file.parent.mkdir(parents=True, exist_ok=True)
        creds = {
            "token": xoxc,
            "cookie": xoxd,
            "team": server["team"],
            "team_id": team_id,
            "user": server["user"],
            "server": server_name,
        }
        creds_file.write_text(json.dumps(creds, indent=2) + "\n")
        os.chmod(str(creds_file), 0o600)
        updated.append(f"creds:{creds_file.name}")
    except OSError as e:
        print(f"  Warning: failed to update {creds_file}: {e}", file=sys.stderr)

    return updated


def show_status() -> None:
    """Show current token status for all servers."""
    print("Slack Token Status")
    print("=" * 70)

    # Load MCP config once
    mcp_cfg = {}
    if MCP_CONFIG.exists():
        try:
            mcp_cfg = json.loads(MCP_CONFIG.read_text()).get("mcpServers", {})
        except json.JSONDecodeError:
            pass

    for name, server in SERVERS.items():
        mcp_name = server["mcp_name"]
        desc = server["description"]

        # MCP token
        mcp_env = mcp_cfg.get(mcp_name, {}).get("env", {})
        mcp_xoxc = mcp_env.get("SLACK_MCP_XOXC_TOKEN", "")
        mcp_status = f"{mcp_xoxc[:20]}..." if mcp_xoxc.startswith("xoxc-") else "MISSING"

        # Creds file token
        creds_file = server["creds_file"]
        if creds_file.exists():
            try:
                creds = json.loads(creds_file.read_text())
                creds_xoxc = creds.get("token", "")
                creds_status = f"{creds_xoxc[:20]}..." if creds_xoxc.startswith("xoxc-") else "INVALID"
                # Check if they match
                match = "SYNCED" if mcp_xoxc == creds_xoxc else "DIVERGED"
            except json.JSONDecodeError:
                creds_status = "CORRUPT"
                match = "N/A"
        else:
            creds_status = "NO FILE"
            match = "N/A"

        # MCP server exists?
        mcp_exists = mcp_name in mcp_cfg

        print(f"\n{name} — {desc}")
        print(f"  MCP server:  {'present' if mcp_exists else 'NOT CONFIGURED'}")
        print(f"  MCP token:   {mcp_status}")
        print(f"  Creds file:  {creds_file.name} → {creds_status}")
        print(f"  Sync:        {match}")
        print(f"  Profile:     {server['profile_dir']}")

    print()


def refresh_server(server_name: str, headless: bool = False) -> bool:
    """Extract fresh tokens for a single server.

    Returns True on success.
    """
    server = SERVERS[server_name]
    print(f"\n{'=' * 60}")
    print(f"Refreshing: {server_name} — {server['description']}")
    print(f"{'=' * 60}")

    tokens = extract_tokens(
        workspace_url=server["workspace"],
        headless=headless,
        profile_dir=server["profile_dir"],
    )

    if not tokens:
        print(f"\nFailed to extract tokens for {server_name}")
        return False

    print(f"\nExtracted tokens for {server_name}:")
    print(f"  XOXC: {tokens['xoxc_token'][:25]}...")
    print(f"  XOXD: {tokens['xoxd_token'][:25]}...")

    updated = store_tokens(server_name, tokens)
    print(f"\nUpdated stores:")
    for store in updated:
        print(f"  ✓ {store}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Extract Slack tokens per MCP server (isolated browser profiles)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Servers come from ~/.config/slack-token-extractor/servers.json;
"all" refreshes every configured server sequentially.

Examples:
  %(prog)s --server slack              # Refresh primary
  %(prog)s --server slack-work         # Refresh another server
  %(prog)s --server all                # Refresh everything
  %(prog)s --status                    # Show current state
  %(prog)s --server slack --headless   # Headless (needs prior login)
        """,
    )
    parser.add_argument(
        "--server",
        "-s",
        choices=list(SERVERS.keys()) + ["all"],
        help="Which MCP server to refresh tokens for",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless mode (requires existing session in the profile)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current token status for all servers",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        help="Override workspace URL (default: per-server config)",
    )

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not args.server:
        parser.error("--server or --status is required")

    targets = list(SERVERS.keys()) if args.server == "all" else [args.server]

    results = {}
    for name in targets:
        if args.workspace:
            SERVERS[name]["workspace"] = args.workspace
        results[name] = refresh_server(name, headless=args.headless)

    print(f"\n{'=' * 60}")
    print("Summary")
    print(f"{'=' * 60}")
    for name, ok in results.items():
        status = "✓ OK" if ok else "✗ FAILED"
        print(f"  {name}: {status}")

    if all(results.values()):
        print("\nDone! Restart Claude Code to pick up new MCP tokens.")
    else:
        failed = [n for n, ok in results.items() if not ok]
        print(f"\nFailed: {', '.join(failed)}. Re-run without --headless to log in.")
        sys.exit(1)


if __name__ == "__main__":
    main()
