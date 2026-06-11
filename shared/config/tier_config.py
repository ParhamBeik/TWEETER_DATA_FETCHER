#!/usr/bin/env python3
"""
Tier and rolling-window policy configuration.

This module keeps account priority metadata and per-priority policies in one
place so historical and live pipelines can share the same scheduling rules.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


DEFAULT_PRIORITY_POLICIES: Dict[int, Dict] = {
    # Highest priority: fastest checks + largest rolling windows.
    1: {
        "poll_interval_seconds": 120,
        "live_window_hours": 48,
        "historical_window_days": 7,
    },
    2: {
        "poll_interval_seconds": 240,
        "live_window_hours": 36,
        "historical_window_days": 5,
    },
    3: {
        "poll_interval_seconds": 360,
        "live_window_hours": 30,
        "historical_window_days": 4,
    },
    4: {
        "poll_interval_seconds": 540,
        "live_window_hours": 24,
        "historical_window_days": 3,
    },
    5: {
        "poll_interval_seconds": 780,
        "live_window_hours": 20,
        "historical_window_days": 2,
    },
    6: {
        "poll_interval_seconds": 1020,
        "live_window_hours": 16,
        "historical_window_days": 1,
    },
    # Default/fallback priority for uncategorized accounts.
    7: {
        "poll_interval_seconds": 1440,
        "live_window_hours": 12,
        "historical_window_days": 1,
    },
}


DEFAULT_TIER_CONFIGURATION: Dict[str, List[Dict[str, str]]] = {
    "priority_1": [
        {"username": "realDonaldTrump", "display_name": "Donald J. Trump"},
        {"username": "SecScottBessent", "display_name": "Scott Bessent"},
        {"username": "USTreasury", "display_name": "US Treasury"},
        {"username": "JDVance", "display_name": "JD Vance"},
        {"username": "TankerTrackers", "display_name": "TankerTrackers"},
        {"username": "chigrl", "display_name": "Tracy Shuchart"},
        {"username": "KobeissiLetter", "display_name": "The Kobeissi Letter"},
        {"username": "AAAnews", "display_name": "AAAnews"},
        {"username": "EIAgov", "display_name": "EIAgov"},
    ],
    "priority_2": [
        {"username": "araghchi", "display_name": "Seyed Abbas Araghchi"},
        {"username": "drpezeshkian", "display_name": "Masoud Pezeshkian"},
        {"username": "MKhamenei_ir", "display_name": "Mojtaba Khamenei"},
        {"username": "SteveWitkoff", "display_name": "Steve Witkoff"},
        {"username": "SecRubio", "display_name": "Marco Rubio"},
        {"username": "LynAldenContact", "display_name": "Lyn Alden"},
        {"username": "LukeGromen", "display_name": "Luke Gromen"},
        {"username": "PeterSchiff", "display_name": "Peter Schiff"},
        {"username": "JimRickards", "display_name": "Jim Rickards"},
        {"username": "business", "display_name": "Bloomberg"},
        {"username": "Reuters", "display_name": "Reuters"},
        {"username": "elonmusk", "display_name": "Elon Musk"},
        {"username": "Lagarde", "display_name": "Christine Lagarde"},
    ],
    "priority_3": [
        {"username": "IRIMFA_SPOX", "display_name": "Esmaeil Baqaei"},
        {"username": "IRIMFA_EN", "display_name": "Iran Foreign Ministry"},
        {"username": "Hemmati_ir", "display_name": "Abdolnaser Hemmati"},
        {"username": "netanyahu", "display_name": "Benjamin Netanyahu"},
        {"username": "Israel_katz", "display_name": "Israel Katz"},
        {"username": "UANI", "display_name": "UANI"},
        {"username": "farnazfassihi", "display_name": "Farnaz Fassihi"},
        {"username": "mdubowitz", "display_name": "Mark Dubowitz"},
        {"username": "rich_goldberg", "display_name": "Richard Goldberg"},
        {"username": "SGhasseminejad", "display_name": "Saeed Ghasseminejad"},
    ],
    "priority_4": [
        {"username": "J_Zarif", "display_name": "J Zarif"},
    ],
    "priority_5": [
        {"username": "IDF", "display_name": "Israel Defense Forces"},
        {"username": "IDFFarsi", "display_name": "IDF Farsi"},
        {"username": "AvichayAdraee", "display_name": "Avichay Adraee"},
        {"username": "DAVIDHALBRIGHT1", "display_name": "David Albright"},
        {"username": "TheGoodISIS", "display_name": "Inst for Science"},
        {"username": "geoconfirmed", "display_name": "GeoConfirmed"},
        {"username": "ronenbergman", "display_name": "Ronen Bergman"},
        {"username": "AmosHarel", "display_name": "Amos Harel"},
        {"username": "ksadjadpour", "display_name": "Karim Sadjadpour"},
        {"username": "vali_nasr", "display_name": "Vali Nasr"},
        {"username": "AliVaez", "display_name": "Ali Vaez"},
    ],
    "priority_6": [
        {"username": "SEPeaceMissions", "display_name": "SE Peace Missions"},
        {"username": "PressSec", "display_name": "Karoline Leavitt"},
        {"username": "gidonsaar", "display_name": "Gideon Saar"},
        {"username": "Shayan86", "display_name": "Shayan Sardarizadeh"},
        {"username": "bellingcat", "display_name": "Bellingcat"},
        {"username": "LauraSecor", "display_name": "Laura Secor"},
        {"username": "MaloneySuzanne", "display_name": "Suzanne Maloney"},
        {"username": "IranIntl", "display_name": "Iran International"},
    ],
    "priority_7": [
        {"username": "BBCVerify", "display_name": "BBC Verify"},
        {"username": "bentallblu", "display_name": "bentallblu"},
        {"username": "HollyDagres", "display_name": "Holly Dagres"},
        {"username": "PahlaviReza", "display_name": "Reza Pahlavi"},
        {"username": "WGC_News", "display_name": "WGC News"},
        {"username": "SantiagoAuFund", "display_name": "Santiago Capital"},
        {"username": "paulkrugman", "display_name": "Paul Krugman"},
        {"username": "RobinBrooksIIF", "display_name": "Robin Brooks"},
        {"username": "elerianm", "display_name": "Mohamed El-Erian"},
        {"username": "NickTimiraos", "display_name": "Nick Timiraos"},
        {"username": "federalreserve", "display_name": "Federal Reserve"},
        {"username": "KitcoNewsNOW", "display_name": "Kitco News"},
        {"username": "GoldTelegraph_", "display_name": "Gold Telegraph"},
        {"username": "flightradar24", "display_name": "Flightradar24"},
        {"username": "RayDalio", "display_name": "Ray Dalio"},
        {"username": "PolymarketIntel", "display_name": "Polymarket Intel"},
    ],
}


def _priority_from_key(key: str) -> int:
    if not key.startswith("priority_"):
        return 7
    try:
        return int(key.split("_", 1)[1])
    except (ValueError, IndexError):
        return 7


def load_tier_config(config: Dict) -> Tuple[Dict[str, Dict], Dict[int, Dict]]:
    """
    Build account->metadata map plus priority policy map.

    Backward compatibility:
    - If `tier_configuration` is absent in config, use module defaults.
    - If some priorities are missing policy overrides, defaults are used.
    """
    tier_cfg = config.get("tier_configuration", DEFAULT_TIER_CONFIGURATION)
    policy_cfg = config.get("priority_policies", {})

    policy_map: Dict[int, Dict] = {}
    for priority, defaults in DEFAULT_PRIORITY_POLICIES.items():
        override = policy_cfg.get(str(priority), {}) or {}
        policy_map[priority] = {
            "priority": priority,
            "poll_interval_seconds": int(override.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            "live_window_hours": int(override.get("live_window_hours", defaults["live_window_hours"])),
            "historical_window_days": int(override.get("historical_window_days", defaults["historical_window_days"])),
        }

    account_map: Dict[str, Dict] = {}
    for key, records in tier_cfg.items():
        priority = _priority_from_key(key)
        if priority not in policy_map:
            priority = 7
        for record in records or []:
            username = str(record.get("username", "")).strip()
            if not username:
                continue
            display_name = str(record.get("display_name") or username).strip() or username
            account_map[username.lower()] = {
                "username": username,
                "display_name": display_name,
                "priority": priority,
            }

    return account_map, policy_map


def get_priority_policy(
    username: str,
    account_map: Dict[str, Dict],
    policy_map: Dict[int, Dict],
) -> Dict:
    """Return policy for username with priority-7 fallback."""
    meta = account_map.get(username.lower())
    priority = meta.get("priority", 7) if meta else 7
    policy = dict(policy_map.get(priority, policy_map[7]))
    policy["username"] = meta.get("username", username) if meta else username
    policy["display_name"] = meta.get("display_name", username) if meta else username
    policy["priority"] = priority
    return policy


def ordered_accounts(account_map: Dict[str, Dict]) -> List[str]:
    """Return usernames by priority while preserving configured order within each tier."""
    rows = list(account_map.values())
    rows.sort(key=lambda row: int(row.get("priority", 7)))
    return [row["username"] for row in rows if row.get("username")]
