import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

ACTIVITIES_CHANNEL_NAME = os.getenv("ACTIVITIES_CHANNEL_NAME", "registro-actividades")
ACTIVITIES_CHANNEL_ID = os.getenv("ACTIVITIES_CHANNEL_ID", "").strip()
VERIFIED_ROLE_NAME = os.getenv("VERIFIED_ROLE_NAME", "Verificado")
HIGH_RANK_ROLE_NAME = os.getenv("HIGH_RANK_ROLE_NAME", "🏅 Gobierno 🏅")

POINTS_PARTICIPANT = 1
POINTS_WINNER = 3
POINTS_GUIDE = 5

REWARDS_CATALOG: dict[str, dict] = {
    "digito_1": {
        "name": "1 Dígito Habbo",
        "cost": 30,
        "period": "Semanal",
        "cooldown_days": 7,
        "emoji": "1️⃣",
        "desc": "Dígito básico. Mínimo de puntos para canjear.",
    },
    "digitos_3": {
        "name": "3 Dígitos Habbo",
        "cost": 80,
        "period": "Semanal",
        "cooldown_days": 7,
        "emoji": "3️⃣",
        "desc": "Paquete de 3 dígitos.",
    },
    "creditos_5": {
        "name": "5 Créditos Habbo",
        "cost": 60,
        "period": "Quincenal",
        "cooldown_days": 15,
        "emoji": "💰",
        "desc": "Créditos para decorar o comprar en Habbo.",
    },
    "creditos_15": {
        "name": "15 Créditos Habbo",
        "cost": 150,
        "period": "Quincenal",
        "cooldown_days": 15,
        "emoji": "💎",
        "desc": "Paquete mayor de créditos Habbo.",
    },
    "membresia_vip": {
        "name": "Membresía VIP",
        "cost": 200,
        "period": "Mensual",
        "cooldown_days": 30,
        "emoji": "👑",
        "desc": "Premio exclusivo de membresía.",
    },
    "caja_hc": {
        "name": "Caja HC",
        "cost": 300,
        "period": "Mensual",
        "cooldown_days": 30,
        "emoji": "📦",
        "desc": "Caja sorpresa HC de Habbo.",
    },
    "membresia_titanium": {
        "name": "Membresía Titanium",
        "cost": 350,
        "period": "Mensual",
        "cooldown_days": 30,
        "emoji": "🛡️",
        "desc": "Premio exclusivo de membresía.",
    },
}

HABBO_ALIASES: dict[str, str] = {
    "rubenandres09": "rubenandres",
    "rubenandre09": "rubenandres",
    "rubenandres": "rubenandres",
    "danopsmash": "dan",
    "danospsmash": "dan",
    "danopsmas": "dan",
    "danopsma": "dan",
    "dan": "dan",
    "100mar100": "100mar",
    "100mar": "100mar",
    "felipefrancis": "felipefrancisc",
    "felipefrancisc": "felipefrancisc",
    "cruzazulista": "cruz",
    "cruz": "cruz",
    "luisito32": "luisito",
    "luisito": "luisito",
    "rudamn": "ruda",
    "ruda": "ruda",
    "walesk": "walezk",
    "walezk": "walezk",
    "xavierfeijoo": "xavierfeijo",
    "xavierjeijo": "xavierfeijo",
    "xavierfeijo": "xavierfeijo",
    "vittoriojoss": "vittorio.joss",
    "vitorrio.joss": "vittorio.joss",
    "vittori.joss": "vittorio.joss",
    "vittorio.joss": "vittorio.joss",
    "nicol1312": "nicole1312",
    "nicole1312": "nicole1312",
    "kali1": "kali_1",
    "kali_1": "kali_1",
    "freddy!": "freddy",
    "freddy": "freddy",
    "juand@": "juanda",
    "juanda": "juanda",
    "leisverjose": "leisver",
    "leiverjose": "leisver",
    "leisver": "leisver",
    "multyreplay": "multyrecplay",
    "multyrecplay": "multyrecplay",
    "dominusursus": "dominusursus",
    "andre": "andre",
    "andré": "andre",
    "habbodavid": "habbo_david",
    "habbo_david": "habbo_david",
    "nicol": ":=nicol=:",
    "=nicol=": ":=nicol=:",
    "milkyway": "milkyway",
    "lyra": "-lyra.-",
    "adrianav": "adrianav",
    "adrianette2": "adrinette2",
    "adrinette2": "adrinette2",
    "ypjn19": "yjpn19",
    "yjpn19": "yjpn19",
    "3n3ria": "3n3ri4",
    "3n3ri4": "3n3ri4",
    "solofelicidad": ".solofelicidad.",
    "juan__2000": "juan2000",
    "juan_2000": "juan2000",
    "juan 2000": "juan2000",
    "juan2000": "juan2000",
    "karladayana12": "karladayana12",
}


def clean_raw_name(text: str) -> str:
    cleaned = re.sub(r"[\u200b-\u200f\ufeff`*]", "", text)
    cleaned = cleaned.replace(r"\_", "_").replace(r"\.", ".")
    cleaned = re.sub(r"\[[^\]]+\]", "", cleaned)
    return cleaned.strip()


def normalize_habbo_key(name: str) -> str:
    base = clean_raw_name(name).casefold()
    stripped = re.sub(r"^[._\-:!@]+|[._\-:!@]+\$", "", base).strip()
    if stripped in HABBO_ALIASES:
        return HABBO_ALIASES[stripped]
    if base in HABBO_ALIASES:
        return HABBO_ALIASES[base]
    return stripped


def is_high_rank(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    target = HIGH_RANK_ROLE_NAME.casefold()
    for role in member.roles:
        role_cf = role.name.casefold()
        if role_cf == target or "gobierno" in role_cf:
            return True
    return False


class PointsStore:
    def __init__(self) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "verification.sqlite3"
        self.path = Path(os.getenv("VERIFICATION_DB", str(default_path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def database(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS verified_links (
                    discord_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    habbo_unique_id TEXT NOT NULL UNIQUE,
                    habbo_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    verified_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS military_points (
                    discord_id INTEGER PRIMARY KEY,
                    habbo_name TEXT NOT NULL COLLATE NOCASE,
                    points_participation INTEGER NOT NULL DEFAULT 0,
                    points_wins INTEGER NOT NULL DEFAULT 0,
                    points_guide INTEGER NOT NULL DEFAULT 0,
                    points_adjusted INTEGER NOT NULL DEFAULT 0,
                    points_spent INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_logs (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    guide_discord_id INTEGER NOT NULL,
                    guide_habbo TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    activity_date TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS redemptions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id INTEGER NOT NULL,
                    habbo_name TEXT NOT NULL,
                    membership_name TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_discord_id INTEGER NOT NULL,
                    sender_habbo TEXT NOT NULL,
                    receiver_discord_id INTEGER NOT NULL,
                    receiver_habbo TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL
                );
                """
            )

    def get_verified_user(self, habbo_name: str):
        cleaned = clean_raw_name(habbo_name)
        target_norm = normalize_habbo_key(cleaned)
        with self.database() as db:
            exact = db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (cleaned,),
            ).fetchone()
            if exact and int(exact["discord_id"]) > 0:
                return exact

            all_rows = db.execute("SELECT * FROM verified_links").fetchall()
            if target_norm:
                for row in all_rows:
                    if int(row["discord_id"]) > 0 and normalize_habbo_key(row["habbo_name"]) == target_norm:
                        return row
                for row in all_rows:
                    if normalize_habbo_key(row["habbo_name"]) == target_norm:
                        return row
            if exact:
                return exact
        return None

    def restore_verified_from_guild(self, discord_id: int, guild_id: int, habbo_name: str):
        cleaned = clean_raw_name(habbo_name)
        with self.database() as db:
            db.execute(
                """INSERT INTO verified_links (discord_id, guild_id, habbo_unique_id, habbo_name, verified_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET
                     habbo_name=excluded.habbo_name""",
                (discord_id, guild_id, f"discord-sync-{discord_id}", cleaned, int(time.time())),
            )
            return db.execute(
                "SELECT * FROM verified_links WHERE discord_id=? LIMIT 1",
                (discord_id,),
            ).fetchone()

    def create_provisional_user(self, habbo_name: str):
        cleaned = clean_raw_name(habbo_name)
        prov_id = -(abs(hash(cleaned.casefold())) % 900_000_000 + 100_000_000)
        with self.database() as db:
            db.execute(
                """INSERT INTO verified_links (discord_id, guild_id, habbo_unique_id, habbo_name, verified_at)
                   VALUES (?, 0, ?, ?, ?)
                   ON CONFLICT(habbo_name) DO NOTHING""",
                (prov_id, f"provisional-{cleaned.casefold()}", cleaned, int(time.time())),
            )
            return db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (cleaned,),
            ).fetchone()

    def merge_provisional_to_discord(self, real_discord_id: int, guild_id: int, habbo_name: str) -> int:
        cleaned = clean_raw_name(habbo_name)
        target_norm = normalize_habbo_key(cleaned)
        merged_count = 0
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            provs = db.execute("SELECT * FROM verified_links WHERE discord_id < 0").fetchall()
            matching_provs = [
                p for p in provs
                if p["habbo_name"].casefold() == cleaned.casefold()
                or (target_norm and normalize_habbo_key(p["habbo_name"]) == target_norm)
            ]

            for prov in matching_provs:
                prov_id = int(prov["discord_id"])
                prov_pts = db.execute(
                    "SELECT * FROM military_points WHERE discord_id=? LIMIT 1",
                    (prov_id,),
                ).fetchone()

                if prov_pts:
                    self._upsert_points(
                        db,
                        real_discord_id,
                        cleaned,
                        part_pts=prov_pts["points_participation"],
                        win_pts=prov_pts["points_wins"],
                        guide_pts=prov_pts["points_guide"],
                        adj_pts=prov_pts["points_adjusted"],
                        spent_pts=prov_pts["points_spent"],
                        now=int(time.time()),
                    )
                    db.execute("DELETE FROM military_points WHERE discord_id=?", (prov_id,))

                db.execute("DELETE FROM verified_links WHERE discord_id=?", (prov_id,))
                merged_count += 1

            db.execute(
                """INSERT INTO verified_links (discord_id, guild_id, habbo_unique_id, habbo_name, verified_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET habbo_name=excluded.habbo_name""",
                (real_discord_id, guild_id, f"discord-sync-{real_discord_id}", cleaned, int(time.time())),
            )
        return merged_count

    def get_verified_by_discord(self, discord_id: int):
        with self.database() as db:
            return db.execute(
                "SELECT * FROM verified_links WHERE discord_id=? LIMIT 1",
                (discord_id,),
            ).fetchone()

    def is_activity_processed(self, message_id: int) -> bool:
        with self.database() as db:
            row = db.execute(
                "SELECT 1 FROM activity_logs WHERE message_id=? LIMIT 1",
                (message_id,),
            ).fetchone()
            return bool(row)

    def record_activity(
        self,
        message_id: int,
        guild_id: int,
        guide: dict,
        game_name: str,
        activity_date: str,
        participants: list[dict],
        winners: list[dict],
        summary_text: str,
    ) -> None:
        now = int(time.time())
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM activity_logs WHERE message_id=?", (message_id,)).fetchone()
            if exists:
                return

            self._upsert_points(db, guide["discord_id"], guide["habbo_name"], guide_pts=POINTS_GUIDE, now=now)

            for p in participants:
                self._upsert_points(db, p["discord_id"], p["habbo_name"], part_pts=POINTS_PARTICIPANT, now=now)

            for w in winners:
                self._upsert_points(db, w["discord_id"], w["habbo_name"], win_pts=POINTS_WINNER, now=now)

            db.execute(
                """INSERT INTO activity_logs
                   (message_id, guild_id, guide_discord_id, guide_habbo, game_name, activity_date, summary_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, guild_id, guide["discord_id"], guide["habbo_name"], game_name, activity_date, summary_text, now),
            )

    def _upsert_points(
        self,
        db: sqlite3.Connection,
        discord_id: int,
        habbo_name: str,
        part_pts: int = 0,
        win_pts: int = 0,
        guide_pts: int = 0,
        adj_pts: int = 0,
        spent_pts: int = 0,
        now: int = 0,
    ) -> None:
        db.execute(
            """INSERT INTO military_points
               (discord_id, habbo_name, points_participation, points_wins, points_guide, points_adjusted, points_spent, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
                 habbo_name = excluded.habbo_name,
                 points_participation = military_points.points_participation + excluded.points_participation,
                 points_wins = military_points.points_wins + excluded.points_wins,
                 points_guide = military_points.points_guide + excluded.points_guide,
                 points_adjusted = military_points.points_adjusted + excluded.points_adjusted,
                 points_spent = military_points.points_spent + excluded.points_spent,
                 updated_at = excluded.updated_at""",
            (discord_id, habbo_name, part_pts, win_pts, guide_pts, adj_pts, spent_pts, now or int(time.time())),
        )

    def get_user_balance(self, discord_id: int):
        with self.database() as db:
            row = db.execute("SELECT * FROM military_points WHERE discord_id=? LIMIT 1", (discord_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            earned = data["points_participation"] + data["points_wins"] + data["points_guide"] + data["points_adjusted"]
            data["earned"] = earned
            data["available"] = earned - data["points_spent"]
            return data

    def check_reward_limit(self, discord_id: int, reward_key: str) -> tuple[bool, int]:
        reward = REWARDS_CATALOG[reward_key]
        cooldown_secs = reward["cooldown_days"] * 86400
        now = int(time.time())
        with self.database() as db:
            row = db.execute(
                """SELECT created_at FROM redemptions_log
                   WHERE discord_id=? AND membership_name=? COLLATE NOCASE
                   ORDER BY created_at DESC LIMIT 1""",
                (discord_id, reward["name"]),
            ).fetchone()
            if not row:
                return True, 0
            next_available = int(row["created_at"]) + cooldown_secs
            if now < next_available:
                return False, next_available
            return True, 0

    def transfer_points(
        self,
        sender_id: int,
        sender_habbo: str,
        receiver_id: int,
        receiver_habbo: str,
        amount: int,
        reason: str,
    ) -> bool:
        now = int(time.time())
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM military_points WHERE discord_id=? LIMIT 1", (sender_id,)).fetchone()
            if not row:
                return False
            earned = row["points_participation"] + row["points_wins"] + row["points_guide"] + row["points_adjusted"]
            available = earned - row["points_spent"]
            if available < amount:
                return False

            self._upsert_points(db, sender_id, sender_habbo, adj_pts=-amount, now=now)
            self._upsert_points(db, receiver_id, receiver_habbo, adj_pts=amount, now=now)

            db.execute(
                """INSERT INTO transfers_log
                   (sender_discord_id, sender_habbo, receiver_discord_id, receiver_habbo, amount, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sender_id, sender_habbo, receiver_id, receiver_habbo, amount, reason, now),
            )
            return True

    def redeem_reward_atomic(self, discord_id: int, habbo_name: str, reward_key: str, admin_id: int) -> tuple[bool, str]:
        reward = REWARDS_CATALOG[reward_key]
