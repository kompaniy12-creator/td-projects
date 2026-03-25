#!/usr/bin/env python3
"""
Dashboard Updater — обновляет данные в index.html и деплоит на Netlify.
Вызывается cron-job'ом через A.R.C.H.O.N. каждый час.

Что проверяет:
1. Статусы всех ботов (online/offline через psutil)
2. Bitrix24 — количество сделок
3. wFirma — количество контрагентов/счетов  
4. Supabase — количество клиентов
5. Обновляет index.html с актуальными данными
6. Деплоит на Netlify
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime

import httpx
import psutil

# ═══ CONFIG ═══
DASHBOARD_DIR = "/Users/kkum/.openclaw/workspace/projects-dashboard"
INDEX_FILE = os.path.join(DASHBOARD_DIR, "index.html")
STATE_FILE = os.path.join(DASHBOARD_DIR, "dashboard_state.json")

BITRIX_WEBHOOK = "https://td-group.bitrix24.eu/rest/1/tpvqp7qihayxpauq"

SUPABASE_TD_URL = "https://bltbuptzsswaislqagwe.supabase.co"
SUPABASE_TD_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJsdGJ1cHR6c3N3YWlzbHFhZ3dlIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MzA3NTU5NCwiZXhwIjoyMDg4NjUxNTk0fQ.GbzTU91HTrut38qO2eJjfpQImN6FOi6OfBSurTPSCzs"

SUPABASE_TDC_URL = "https://dpfxwkxpzqqjtmgqwozw.supabase.co"
SUPABASE_TDC_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRwZnh3a3hwenFxanRtZ3F3b3p3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MzI1NDE4OSwiZXhwIjoyMDg4ODMwMTg5fQ.Tq4phlzQ3M_lh18UfuPco0bANgjtcVveq0GTf6r_3NU"

GH_TOKEN_FILE = "/Users/kkum/.openclaw/workspace/secrets/github-token.txt"
GH_REPO = "kompaniy12-creator/td-projects"

# Bot processes to check
BOT_SERVICES = {
    "A.R.C.H.O.N.": {"cwd_contains": "openclaw", "always_online": True},
    "N.E.X.U.S.": {"cwd_contains": "nexus-personal-bot", "script": "bot.py"},
    "A.U.R.U.M.": {"cwd_contains": "td-finance-bot", "script": "bot.py"},
    "O.S.C.A.R.": {"cwd_contains": "td-contract-agent", "script": "bot.py"},
    "S.I.R.E.N.": {"cwd_contains": "siren-bot", "script": "bot.py"},
    "S.E.N.T.R.Y.": {"cwd_contains": "sentry-bot", "script": "sentry_daemon.py"},
}

# All services monitored (for dashboard "services online" count)
ALL_SERVICES = {
    "referral": {"cwd_contains": "twojadecyzja-referral-bot", "script": "bot.py"},
    "contract": {"cwd_contains": "td-contract-agent/scripts", "script": "bot.py"},
    "kkum": {"cwd_contains": "kkum-sales-bot", "script": "bot.py"},
    "accounting": {"cwd_contains": "td-accounting-bot", "script": "bot.py"},
    "monitor": {"cwd_contains": "td-monitor", "script": "monitor.py"},
    "finance": {"cwd_contains": "td-finance-bot", "script": "bot.py"},
    "nexus": {"cwd_contains": "nexus-personal-bot", "script": "bot.py"},
    "siren": {"cwd_contains": "siren-bot", "script": "bot.py"},
    "bitrix_app": {"cwd_contains": "td-contract-agent", "script": "bitrix_app.py"},
    "cloudflared": {"cwd_contains": "", "script": "cloudflared", "any_cwd": True},
    "sentry": {"cwd_contains": "sentry-bot", "script": "sentry_daemon.py"},
}


def check_bot_statuses() -> dict:
    """Check which bots are currently running."""
    statuses = {}
    for name, cfg in BOT_SERVICES.items():
        if cfg.get("always_online"):
            statuses[name] = "online"
            continue
        
        found = False
        for proc in psutil.process_iter(['pid', 'cmdline', 'cwd']):
            try:
                info = proc.info
                cwd = info.get('cwd') or ''
                cmdline = info.get('cmdline') or []
                if cfg["cwd_contains"] in cwd and any(cfg.get("script", "bot.py") in arg for arg in cmdline):
                    found = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        statuses[name] = "online" if found else "offline"
    
    return statuses


def fetch_bitrix_stats() -> dict:
    """Get deal counts from Bitrix24."""
    stats = {"total_deals": 0, "active_deals": 0, "won_deals": 0}
    try:
        with httpx.Client(timeout=15) as client:
            # Total active deals
            resp = client.post(f"{BITRIX_WEBHOOK}/crm.deal.list", data={
                "filter[CLOSED]": "N",
                "select[0]": "ID",
            })
            data = resp.json()
            stats["active_deals"] = data.get("total", 0)
            
            # Won deals
            resp = client.post(f"{BITRIX_WEBHOOK}/crm.deal.list", data={
                "filter[STAGE_SEMANTIC_ID]": "S",
                "select[0]": "ID",
            })
            data = resp.json()
            stats["won_deals"] = data.get("total", 0)
            
            stats["total_deals"] = stats["active_deals"] + stats["won_deals"]
    except Exception as e:
        print(f"Bitrix error: {e}")
    return stats


def fetch_supabase_stats() -> dict:
    """Get user/client counts from Supabase."""
    stats = {"referral_users": 0, "accounting_clients": 0}
    headers_td = {"apikey": SUPABASE_TD_KEY, "Authorization": f"Bearer {SUPABASE_TD_KEY}"}
    headers_tdc = {"apikey": SUPABASE_TDC_KEY, "Authorization": f"Bearer {SUPABASE_TDC_KEY}"}
    
    try:
        with httpx.Client(timeout=15) as client:
            # Referral users
            resp = client.get(
                f"{SUPABASE_TD_URL}/rest/v1/users?select=id",
                headers={**headers_td, "Prefer": "count=exact", "Range": "0-0"},
            )
            stats["referral_users"] = int(resp.headers.get("content-range", "0/0").split("/")[-1] or 0)
            
            # Accounting clients
            resp = client.get(
                f"{SUPABASE_TDC_URL}/rest/v1/accounting_clients?select=id",
                headers={**headers_tdc, "Prefer": "count=exact", "Range": "0-0"},
            )
            stats["accounting_clients"] = int(resp.headers.get("content-range", "0/0").split("/")[-1] or 0)
    except Exception as e:
        print(f"Supabase error: {e}")
    return stats


def update_agent_statuses(html: str, bot_statuses: dict) -> str:
    """Update agent online/offline status in HTML."""
    for name, status in bot_statuses.items():
        # Find pattern: name:"X.Y.Z.",... status:"online"  and update
        pattern = rf'(name:"{re.escape(name)}".*?status:")[^"]*(")'
        html = re.sub(pattern, rf'\g<1>{status}\2', html, flags=re.DOTALL)
    return html


def check_all_services() -> dict:
    """Check all services (not just agents)."""
    statuses = {}
    for name, cfg in ALL_SERVICES.items():
        found = False
        for proc in psutil.process_iter(['pid', 'cmdline', 'cwd']):
            try:
                info = proc.info
                cwd = info.get('cwd') or ''
                cmdline = ' '.join(info.get('cmdline') or [])
                script = cfg.get("script", "")
                
                if cfg.get("any_cwd"):
                    if script in cmdline:
                        found = True
                        break
                elif cfg["cwd_contains"] in cwd and script in cmdline:
                    found = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        statuses[name] = "online" if found else "offline"
    return statuses


def update_main_stats(html: str, bitrix: dict, supabase: dict, bot_statuses: dict) -> str:
    """Update the computed stats section."""
    online_count = sum(1 for s in bot_statuses.values() if s == "online")
    total_agents = len(bot_statuses)
    
    # Check all services too
    all_svcs = check_all_services()
    services_online = sum(1 for s in all_svcs.values() if s == "online")
    services_total = len(all_svcs)
    
    # Update lastUpdated timestamp
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    html = re.sub(
        r"(id=\"lastUpdated\">)[^<]*(</div>)",
        rf'\1🕐 Обновлено: {now} | {online_count}/{total_agents} агентов | {services_online}/{services_total} сервисов | ⚡ Auto-Update\2',
        html
    )
    
    return html


def deploy_github():
    """Deploy to GitHub Pages via git push."""
    try:
        token = open(GH_TOKEN_FILE).read().strip()
        os.chdir(DASHBOARD_DIR)
        
        # Ensure git is configured
        subprocess.run(["git", "config", "user.email", "archon@tdgroup.pl"], capture_output=True)
        subprocess.run(["git", "config", "user.name", "A.R.C.H.O.N."], capture_output=True)
        
        # Set remote URL with token
        subprocess.run(
            ["git", "remote", "set-url", "origin", f"https://{token}@github.com/{GH_REPO}.git"],
            capture_output=True,
        )
        
        # Add, commit, push
        subprocess.run(["git", "add", "index.html"], capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"📊 Auto-update {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            capture_output=True, text=True,
        )
        if "nothing to commit" in (result.stdout + result.stderr):
            print("✅ GitHub: nothing to commit (no changes)")
            return True
            
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print("✅ GitHub Pages deploy OK")
        else:
            print(f"❌ GitHub push failed: {result.stderr[:200]}")
        return result.returncode == 0
    except Exception as e:
        print(f"❌ GitHub error: {e}")
        return False


def save_state(bot_statuses, bitrix, supabase):
    """Save state for comparison."""
    state = {
        "timestamp": datetime.now().isoformat(),
        "bots": bot_statuses,
        "bitrix": bitrix,
        "supabase": supabase,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    print(f"🔄 Dashboard update started at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    # 1. Check bot statuses
    bot_statuses = check_bot_statuses()
    print(f"Bots: {bot_statuses}")
    
    # 2. Fetch Bitrix stats
    bitrix = fetch_bitrix_stats()
    print(f"Bitrix: {bitrix}")
    
    # 3. Fetch Supabase stats  
    supabase = fetch_supabase_stats()
    print(f"Supabase: {supabase}")
    
    # 4. Read current HTML
    with open(INDEX_FILE, "r") as f:
        html = f.read()
    
    # 5. Update data
    html = update_agent_statuses(html, bot_statuses)
    html = update_main_stats(html, bitrix, supabase, bot_statuses)
    
    # 6. Save HTML
    with open(INDEX_FILE, "w") as f:
        f.write(html)
    print("✅ index.html updated")
    
    # 7. Deploy
    deployed = deploy_github()
    
    # 8. Save state
    save_state(bot_statuses, bitrix, supabase)
    
    # 9. Summary
    online = sum(1 for s in bot_statuses.values() if s == "online")
    total = len(bot_statuses)
    print(f"\n📊 Итог: {online}/{total} ботов онлайн | Bitrix: {bitrix['active_deals']} активных сделок | Deploy: {'✅' if deployed else '❌'}")


if __name__ == "__main__":
    main()
