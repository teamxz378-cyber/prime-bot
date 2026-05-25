"""
PRIME Discord Bot — подписки + модерация
Команды подписок:
  /sub @user [дней] — добавить подписку + выдать роль
  /unsub @user — убрать подписку + снять роль
  /check @user — проверить подписку
  /myinfo — посмотреть свою подписку
  /hwid_reset @user — сбросить HWID
  /subs — список всех подписчиков

Команды модерации:
  /warn @user [причина] — предупреждение (3 варна = мут, 5 = бан)
  /mute @user [минут] [причина] — замутить
  /unmute @user — размутить
  /kick @user [причина] — кикнуть
  /ban @user [причина] — забанить
  /warns @user — посмотреть варны
  /clearwarns @user — очистить варны

Автомод:
  - Антиспам (5+ сообщений за 5 сек)
  - Антимат (фильтр плохих слов)
  - Антиссылки (discord.gg, http://, https://)
  - Антикапс (80%+ капса при 10+ символах)
  - Антирейд (3+ пользователей заходят за 10 сек)
"""

import os
import ssl
import uuid
import hashlib

# Fix SSL for Termux — must be BEFORE importing discord
try:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    # Monkey-patch so discord.py also uses our SSL context
    _original_create_default_context = ssl.create_default_context
    def _patched_create_default_context(*args, **kwargs):
        if "cafile" not in kwargs and "capath" not in kwargs and "cadata" not in kwargs:
            kwargs["cafile"] = certifi.where()
        return _original_create_default_context(*args, **kwargs)
    ssl.create_default_context = _patched_create_default_context
    print("[SSL] Using certifi certificates:", certifi.where())
except ImportError:
    ssl_ctx = ssl.create_default_context()
    print("[SSL] certifi not found, using system certificates")

import discord
from discord import app_commands
import aiohttp
import datetime
import json
import re
import asyncio
from collections import defaultdict
from pathlib import Path

# ─── CONFIG ───────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GUILD_ID = 1497511921140629565
SUB_ROLE_ID = 1503792748690407527  # ⚡ Клиент
LOG_CHANNEL_ID = 1503793060251959346  # 📝│логи
API_URL = os.environ.get("API_URL", "http://localhost:8000")
API_KEY = "prime-secret-2025"

# Update channel (created automatically in "Система" category)
UPDATE_CHANNEL_NAME = "🔄│обновления"
UPDATE_CHANNEL_ID = None  # set on_ready

# Game logs channel
GAME_LOGS_CHANNEL_NAME = "логи-и-управление"
GAME_LOGS_CHANNEL_ID = None  # set on_ready

# Base launcher .exe path (with placeholder token for binary patching)
BASE_LAUNCHER_URL = os.environ.get("BASE_LAUNCHER_URL", "")
BASE_LAUNCHER_PATH = Path("data/PrimeLauncher-base.exe")
TOKEN_PLACEHOLDER = b"%%PRIME_USER_TOKEN_PLACEHOLDER%%"

# Category for personal user channels
PERSONAL_CAT_NAME = "📦 КЛИЕНТЫ"

# Список Discord ID админов
ADMIN_IDS = []

# Каналы, игнорируемые автомодом (система)
IGNORED_CHANNELS = {
    1503356481607831593,   # хвид
    1503433534059057353,   # пароли
    1503777062983438336,   # подписки
    1503793060251959346,   # логи
}

# ─── AUTOMOD CONFIG ───────────────────────────────────────────
BAD_WORDS = [
    "бля", "блять", "сука", "пизд", "хуй", "хуя", "хуе", "ебат", "ебан",
    "ёбан", "еба", "нахуй", "нахуя", "пидор", "пидр", "мудак", "мудил",
    "залуп", "шлюх", "долбоёб", "долбоеб", "дебил", "уёбок", "уебок",
    "ублюд", "гандон", "ганд", "манда", "елда", "хер ", "похер",
]

LINK_PATTERN = re.compile(
    r'(https?://|discord\.gg/|discordapp\.com/invite/|t\.me/|bit\.ly/)', re.IGNORECASE
)

# Spam: max messages in window
SPAM_MAX_MESSAGES = 5
SPAM_WINDOW_SEC = 5

# Caps: min length and threshold
CAPS_MIN_LENGTH = 10
CAPS_THRESHOLD = 0.8

# Raid: joins in window
RAID_JOIN_LIMIT = 3
RAID_WINDOW_SEC = 10

# Warn thresholds
WARN_MUTE_THRESHOLD = 3     # 3 warns = auto mute
WARN_BAN_THRESHOLD = 5      # 5 warns = auto ban
AUTO_MUTE_MINUTES = 30       # auto mute duration

# ─── DATA STORAGE ─────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
WARNS_FILE = DATA_DIR / "warns.json"
CONFIGS_FILE = DATA_DIR / "configs.json"
CONFIGS_DIR = DATA_DIR / "configs"

def load_configs():
    if CONFIGS_FILE.exists():
        try:
            return json.loads(CONFIGS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_configs(configs):
    DATA_DIR.mkdir(exist_ok=True)
    CONFIGS_FILE.write_text(json.dumps(configs, indent=2, ensure_ascii=False))

def generate_config_key():
    return uuid.uuid4().hex[:8].upper()

configs_db = load_configs()

def load_warns():
    if WARNS_FILE.exists():
        try:
            return json.loads(WARNS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_warns(warns):
    DATA_DIR.mkdir(exist_ok=True)
    WARNS_FILE.write_text(json.dumps(warns, indent=2, ensure_ascii=False))

warns_db = load_warns()

# ─── TRACKING ─────────────────────────────────────────────────
message_timestamps = defaultdict(list)  # user_id -> [timestamps]
join_timestamps = []                     # [(timestamp, member)]

# ─── Bot Setup ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if interaction.user.id in ADMIN_IDS:
        return True
    return False


def is_mod(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.guild_permissions.manage_messages:
        return True
    if member.id in ADMIN_IDS:
        return True
    return False


async def api_request(method: str, path: str, json_data=None):
    headers = {"X-Api-Key": API_KEY}
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            if method == "GET":
                async with session.get(f"{API_URL}{path}", headers=headers) as resp:
                    return await resp.json()
            elif method == "POST":
                async with session.post(f"{API_URL}{path}", headers=headers, json=json_data) as resp:
                    return await resp.json()
    except Exception as e:
        print(f"[API ERROR] {e}")
        return {"error": "API unavailable"}


async def api_upload_mod(file_bytes: bytes, filename: str, version: str = "", changelog: str = ""):
    """Upload a .jar to the API server."""
    headers = {"X-Api-Key": API_KEY}
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    try:
        data = aiohttp.FormData()
        data.add_field("file", file_bytes, filename=filename, content_type="application/java-archive")
        if version:
            data.add_field("version", version)
        if changelog:
            data.add_field("changelog", changelog)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(f"{API_URL}/api/mod/upload", headers=headers, data=data) as resp:
                return await resp.json()
    except Exception as e:
        print(f"[API UPLOAD ERROR] {e}")
        return {"error": str(e)}


def format_expires(ts: float) -> str:
    if ts == 0:
        return "Бессрочно"
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def format_remaining(ts: float) -> str:
    if ts == 0:
        return "∞"
    remaining = ts - datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
    if remaining <= 0:
        return "Истекла"
    days = int(remaining / 86400)
    hours = int((remaining % 86400) / 3600)
    return f"{days}д {hours}ч"


# ─── LOGGING ──────────────────────────────────────────────────
async def send_log(guild: discord.Guild, embed: discord.Embed):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception:
            pass


async def log_action(guild, action: str, moderator, target, reason: str = None, duration: str = None, color: int = 0xff9900):
    embed = discord.Embed(
        title=f"⚙️ {action}",
        color=color,
        timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
    )
    embed.add_field(name="Пользователь", value=f"{target.mention} (`{target.id}`)", inline=True)
    if moderator:
        mod_name = moderator.mention if hasattr(moderator, 'mention') else str(moderator)
        embed.add_field(name="Модератор", value=mod_name, inline=True)
    if reason:
        embed.add_field(name="Причина", value=reason, inline=False)
    if duration:
        embed.add_field(name="Длительность", value=duration, inline=True)
    embed.set_footer(text="PRIME Moderation")
    await send_log(guild, embed)


# ─── WARN SYSTEM ──────────────────────────────────────────────
def add_warn(user_id: int, reason: str, moderator_id: int) -> int:
    uid = str(user_id)
    if uid not in warns_db:
        warns_db[uid] = []
    warns_db[uid].append({
        "reason": reason,
        "moderator": moderator_id,
        "time": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    })
    save_warns(warns_db)
    return len(warns_db[uid])


def get_warns(user_id: int) -> list:
    return warns_db.get(str(user_id), [])


def clear_warns(user_id: int):
    uid = str(user_id)
    if uid in warns_db:
        del warns_db[uid]
        save_warns(warns_db)


# ─── AUTOMOD ──────────────────────────────────────────────────
def check_bad_words(content: str) -> bool:
    lower = content.lower()
    for word in BAD_WORDS:
        if word in lower:
            return True
    return False


def check_links(content: str) -> bool:
    return bool(LINK_PATTERN.search(content))


def check_caps(content: str) -> bool:
    letters = [c for c in content if c.isalpha()]
    if len(letters) < CAPS_MIN_LENGTH:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) >= CAPS_THRESHOLD


def check_spam(user_id: int) -> bool:
    now = datetime.datetime.now().timestamp()
    timestamps = message_timestamps[user_id]
    timestamps.append(now)
    # Clean old timestamps
    cutoff = now - SPAM_WINDOW_SEC
    message_timestamps[user_id] = [t for t in timestamps if t > cutoff]
    return len(message_timestamps[user_id]) > SPAM_MAX_MESSAGES


async def automod_action(message: discord.Message, violation: str):
    """Handle automod violation: delete message, warn user, escalate if needed."""
    # Delete message
    try:
        await message.delete()
    except Exception:
        pass

    member = message.author
    guild = message.guild

    # Add warn
    total_warns = add_warn(member.id, f"Автомод: {violation}", bot.user.id)

    # Notify in channel
    notify = await message.channel.send(
        f"⚠️ {member.mention} — {violation} (предупреждение {total_warns}/{WARN_BAN_THRESHOLD})",
    )
    asyncio.create_task(delete_after(notify, 10))

    # Log
    await log_action(guild, f"Автомод — {violation}", bot.user, member,
                      reason=f"Сообщение удалено | Варн #{total_warns}", color=0xff9900)

    # Auto-escalate
    if total_warns >= WARN_BAN_THRESHOLD:
        try:
            await member.ban(reason=f"Автомод: {WARN_BAN_THRESHOLD} предупреждений")
            await log_action(guild, "Автобан", bot.user, member,
                             reason=f"Достигнут лимит ({WARN_BAN_THRESHOLD} варнов)", color=0xff0000)
        except Exception:
            pass
    elif total_warns >= WARN_MUTE_THRESHOLD:
        try:
            until = discord.utils.utcnow() + datetime.timedelta(minutes=AUTO_MUTE_MINUTES)
            await member.timeout(until, reason=f"Автомод: {WARN_MUTE_THRESHOLD} предупреждений")
            await log_action(guild, "Автомут", bot.user, member,
                             reason=f"Достигнут лимит ({WARN_MUTE_THRESHOLD} варнов)",
                             duration=f"{AUTO_MUTE_MINUTES} мин", color=0xff6600)
        except Exception:
            pass


async def delete_after(msg, seconds):
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
    except Exception:
        pass


# ─── SUBSCRIPTION COMMANDS ────────────────────────────────────
# ─── PERSONAL CHANNEL + LAUNCHER ──────────────────────────────
async def create_personal_channel(guild: discord.Guild, user: discord.Member) -> discord.TextChannel | None:
    """Create a personal channel for the user in 📦 КЛИЕНТЫ category."""
    try:
        channels = await guild.fetch_channels()
    except Exception:
        channels = guild.channels or []

    # Find or create category
    personal_cat = None
    for ch in channels:
        if isinstance(ch, discord.CategoryChannel) and "клиент" in ch.name.lower():
            personal_cat = ch
            break
    if personal_cat is None:
        try:
            personal_cat = await guild.create_category(PERSONAL_CAT_NAME)
        except Exception as e:
            print(f"[PERSONAL] Failed to create category: {e}")
            return None

    # Check if channel already exists for this user
    channel_name = f"🔑│{user.name}"
    for ch in channels:
        if isinstance(ch, discord.TextChannel) and hasattr(ch, 'category_id') and ch.category_id == personal_cat.id:
            if user.name.lower() in ch.name.lower():
                return ch  # Already exists

    # Create channel visible only to the user + admins + bot
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
    }
    try:
        ch = await guild.create_text_channel(
            channel_name, category=personal_cat, overwrites=overwrites,
            topic=f"Персональный лоадер для {user.display_name}. Скачай PrimeLauncher.exe и запусти!"
        )
        IGNORED_CHANNELS.add(ch.id)
        return ch
    except Exception as e:
        print(f"[PERSONAL] Failed to create channel for {user}: {e}")
        return None


async def send_personalized_launcher(channel: discord.TextChannel, user: discord.Member, user_token: str):
    """Patch the base launcher with user's token and send to their personal channel."""
    if not BASE_LAUNCHER_PATH.exists():
        # Try to download from URL if configured
        if BASE_LAUNCHER_URL:
            try:
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(BASE_LAUNCHER_URL) as resp:
                        if resp.status == 200:
                            BASE_LAUNCHER_PATH.parent.mkdir(parents=True, exist_ok=True)
                            BASE_LAUNCHER_PATH.write_bytes(await resp.read())
            except Exception as e:
                print(f"[LAUNCHER] Failed to download base launcher: {e}")

    if not BASE_LAUNCHER_PATH.exists():
        await channel.send("⚠️ Базовый лоадер не найден. Обратитесь к администратору.")
        return

    # Read base launcher and patch token
    exe_data = BASE_LAUNCHER_PATH.read_bytes()
    if TOKEN_PLACEHOLDER not in exe_data:
        await channel.send("⚠️ Ошибка: плейсхолдер токена не найден в лоадере.")
        return

    # Pad token to match placeholder length (34 chars = len("%%PRIME_USER_TOKEN_PLACEHOLDER%%"))
    padded_token = user_token.ljust(len(TOKEN_PLACEHOLDER)).encode('ascii')[:len(TOKEN_PLACEHOLDER)]
    patched_data = exe_data.replace(TOKEN_PLACEHOLDER, padded_token, 1)

    # Save to temp file and send
    import tempfile
    tmp_path = Path(tempfile.mktemp(suffix=".exe", prefix=f"PrimeLauncher-{user.name}-"))
    tmp_path.write_bytes(patched_data)

    try:
        embed = discord.Embed(
            title="🎮 PRIME Client — Твой лоадер",
            description=(
                f"Привет, {user.mention}! Вот твой персональный лоадер.\n\n"
                "**Инструкция:**\n"
                "1. Скачай `PrimeLauncher.exe` ниже\n"
                "2. Запусти его\n"
                "3. HWID привяжется автоматически\n"
                "4. Нажми **Запустить**\n\n"
                "⚠️ **Не передавай этот файл другим!** Он привязан к твоей подписке."
            ),
            color=0xa94cef,
        )
        embed.set_footer(text="PRIME Client System")
        await channel.send(embed=embed)
        await channel.send(file=discord.File(str(tmp_path), filename="PrimeLauncher.exe"))
    except Exception as e:
        print(f"[LAUNCHER] Failed to send launcher: {e}")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


@tree.command(name="sub", description="Добавить подписку пользователю")
@app_commands.describe(user="Пользователь", days="Количество дней (0 = бессрочно)")
async def cmd_sub(interaction: discord.Interaction, user: discord.Member, days: int = 30):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только админы могут управлять подписками.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    result = await api_request("POST", "/api/subscribe", {
        "discord_id": str(user.id),
        "username": str(user),
        "days": days,
    })

    if result.get("ok"):
        guild = interaction.guild
        role = guild.get_role(SUB_ROLE_ID)
        if role:
            try:
                await user.add_roles(role)
            except discord.Forbidden:
                pass

        user_token = result.get("user_token", "")

        # Create personal channel for this user
        personal_ch = await create_personal_channel(guild, user)

        # Generate personalized launcher and send to personal channel
        if personal_ch and user_token:
            await send_personalized_launcher(personal_ch, user, user_token)

        exp_str = format_expires(result.get("expires", 0))
        embed = discord.Embed(title="✅ Подписка добавлена", color=0xa94cef)
        embed.add_field(name="Пользователь", value=f"{user.mention}", inline=True)
        embed.add_field(name="Срок", value=f"{days} дней" if days > 0 else "Бессрочно", inline=True)
        embed.add_field(name="Истекает", value=exp_str, inline=True)
        if personal_ch:
            embed.add_field(name="Канал", value=f"{personal_ch.mention}", inline=True)
        embed.set_footer(text=f"Discord ID: {user.id}")
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send("❌ Ошибка API", ephemeral=True)


@tree.command(name="unsub", description="Убрать подписку у пользователя")
@app_commands.describe(user="Пользователь")
async def cmd_unsub(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только админы.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    await api_request("POST", "/api/unsubscribe", {"discord_id": str(user.id)})

    guild = interaction.guild
    role = guild.get_role(SUB_ROLE_ID)
    if role:
        try:
            await user.remove_roles(role)
        except discord.Forbidden:
            pass

    # Delete personal channel
    deleted_channel = False
    if guild:
        try:
            channels = await guild.fetch_channels()
        except Exception:
            channels = guild.channels or []
        personal_cat = None
        for ch in channels:
            if isinstance(ch, discord.CategoryChannel) and "клиент" in ch.name.lower():
                personal_cat = ch
                break
        if personal_cat:
            for ch in channels:
                if isinstance(ch, discord.TextChannel) and hasattr(ch, 'category_id') and ch.category_id == personal_cat.id:
                    if user.name.lower() in ch.name.lower():
                        try:
                            await ch.delete(reason=f"Подписка снята у {user}")
                            deleted_channel = True
                        except Exception as e:
                            print(f"[UNSUB] Failed to delete channel {ch.name}: {e}")
                        break

    embed = discord.Embed(
        title="🗑️ Подписка удалена",
        description=f"{user.mention} — подписка снята" + (" • канал удалён" if deleted_channel else ""),
        color=0xff5555,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="check", description="Проверить подписку пользователя")
@app_commands.describe(user="Пользователь")
async def cmd_check(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    result = await api_request("GET", f"/api/check/{user.id}")

    if result.get("subscribed"):
        embed = discord.Embed(title="✅ Подписка активна", color=0x50dc8c)
        embed.add_field(name="Пользователь", value=f"{user.mention}", inline=True)
        embed.add_field(name="Истекает", value=format_expires(result.get("expires", 0)), inline=True)
        embed.add_field(name="Осталось", value=format_remaining(result.get("expires", 0)), inline=True)
    elif result.get("expired"):
        embed = discord.Embed(
            title="⏰ Подписка истекла",
            description=f"{user.mention} — подписка закончилась",
            color=0xff9900,
        )
    else:
        embed = discord.Embed(
            title="❌ Нет подписки",
            description=f"{user.mention} — подписка не найдена",
            color=0xff5555,
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="myinfo", description="Посмотреть свою подписку")
async def cmd_myinfo(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    result = await api_request("GET", f"/api/check/{interaction.user.id}")

    if result.get("subscribed"):
        embed = discord.Embed(title="📋 Твоя подписка", color=0xa94cef)
        embed.add_field(name="Статус", value="✅ Активна", inline=True)
        embed.add_field(name="Истекает", value=format_expires(result.get("expires", 0)), inline=True)
        embed.add_field(name="Осталось", value=format_remaining(result.get("expires", 0)), inline=True)
        embed.add_field(name="Discord ID", value=str(interaction.user.id), inline=False)
    else:
        embed = discord.Embed(
            title="📋 Твоя подписка",
            description="❌ У тебя нет активной подписки",
            color=0xff5555,
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="hwid", description="Привязать свой HWID (скопируй из лаунчера)")
@app_commands.describe(hwid="Твой HWID из лаунчера")
async def cmd_hwid(interaction: discord.Interaction, hwid: str):
    """User submits their HWID from the launcher to bind it to their Discord account."""
    await interaction.response.defer(ephemeral=True)

    hwid = hwid.strip()
    if not hwid or len(hwid) < 8:
        await interaction.followup.send("❌ Неверный HWID. Скопируй его из лаунчера.", ephemeral=True)
        return

    # First check if user has a subscription
    check_result = await api_request("GET", f"/api/check/{interaction.user.id}")

    if not check_result.get("subscribed"):
        if check_result.get("expired"):
            await interaction.followup.send("⏰ Твоя подписка истекла. Обратись к админу для продления.", ephemeral=True)
        else:
            await interaction.followup.send("❌ У тебя нет активной подписки. Сначала попроси админа выдать подписку через /sub.", ephemeral=True)
        return

    # Bind HWID via API
    result = await api_request("POST", "/api/hwid/bind", {
        "discord_id": str(interaction.user.id),
        "hwid": hwid,
    })

    if result.get("ok"):
        embed = discord.Embed(
            title="✅ HWID привязан",
            description=f"Теперь можешь запускать клиент через лаунчер!",
            color=0x50dc8c,
        )
        embed.add_field(name="HWID", value=f"`{hwid[:16]}...`", inline=True)
        embed.add_field(name="Discord ID", value=str(interaction.user.id), inline=True)
        embed.set_footer(text="Если сменишь ПК — попроси админа сбросить HWID через /hwid_reset")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Log the binding
        guild = interaction.guild
        if guild:
            log_embed = discord.Embed(
                title="🔗 HWID привязан",
                color=0x50dc8c,
                timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
            )
            log_embed.add_field(name="Пользователь", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
            log_embed.add_field(name="HWID", value=f"`{hwid[:16]}...`", inline=True)
            log_embed.set_footer(text="PRIME HWID System")
            await send_log(guild, log_embed)
    elif result.get("error") == "hwid_already_bound":
        await interaction.followup.send(
            f"❌ Этот HWID уже привязан к другому аккаунту. Обратись к админу.",
            ephemeral=True,
        )
    else:
        error_msg = result.get("error", "Неизвестная ошибка")
        await interaction.followup.send(f"❌ Ошибка: {error_msg}", ephemeral=True)


@tree.command(name="hwid_reset", description="Сбросить HWID пользователя")
@app_commands.describe(user="Пользователь")
async def cmd_hwid_reset(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только админы.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    result = await api_request("POST", "/api/hwid/reset", {"discord_id": str(user.id)})

    if result.get("ok") and result.get("reset"):
        embed = discord.Embed(
            title="🔄 HWID сброшен",
            description=f"{user.mention} — HWID очищен, можно привязать заново",
            color=0xdaa520,
        )
    else:
        embed = discord.Embed(
            title="❌ Не найден",
            description=f"{user.mention} — подписка не найдена",
            color=0xff5555,
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="give", description="Выдать подписку по нику (Discord или MC)")
@app_commands.describe(nickname="Ник пользователя (Discord username или display name)", days="Количество дней (0 = бессрочно)")
async def cmd_give(interaction: discord.Interaction, nickname: str, days: int = 30):
    """Admin gives subscription by typing a nickname instead of @mention."""
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только админы.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    nickname = nickname.strip()
    guild = interaction.guild

    # Find member by display name, username, or global name
    found_member = None
    for member in guild.members:
        if (member.display_name.lower() == nickname.lower() or
            member.name.lower() == nickname.lower() or
            (member.global_name and member.global_name.lower() == nickname.lower())):
            found_member = member
            break

    if not found_member:
        # Try partial match
        for member in guild.members:
            if (nickname.lower() in member.display_name.lower() or
                nickname.lower() in member.name.lower()):
                found_member = member
                break

    if not found_member:
        await interaction.followup.send(
            f"❌ Пользователь с ником `{nickname}` не найден на сервере.",
            ephemeral=True,
        )
        return

    # Subscribe via API
    result = await api_request("POST", "/api/subscribe", {
        "discord_id": str(found_member.id),
        "username": str(found_member),
        "days": days,
    })

    if result.get("ok"):
        # Give role
        role = guild.get_role(SUB_ROLE_ID)
        if role:
            try:
                await found_member.add_roles(role)
            except discord.Forbidden:
                pass

        user_token = result.get("user_token", "")

        # Create personal channel and send launcher
        personal_ch = await create_personal_channel(guild, found_member)
        if personal_ch and user_token:
            await send_personalized_launcher(personal_ch, found_member, user_token)

        exp_str = format_expires(result.get("expires", 0))
        embed = discord.Embed(title="✅ Подписка выдана", color=0xa94cef)
        embed.add_field(name="Пользователь", value=f"{found_member.mention} (`{found_member}`)", inline=True)
        embed.add_field(name="Срок", value=f"{days} дней" if days > 0 else "Бессрочно", inline=True)
        embed.add_field(name="Истекает", value=exp_str, inline=True)
        if personal_ch:
            embed.add_field(name="Канал", value=f"{personal_ch.mention}", inline=True)
        embed.add_field(name="HWID", value="Авто-привяжется при первом запуске лаунчера", inline=False)
        embed.set_footer(text=f"Найден по нику: {nickname}")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Log
        log_embed = discord.Embed(
            title="🎫 Подписка выдана (по нику)",
            color=0xa94cef,
            timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
        )
        log_embed.add_field(name="Админ", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Пользователь", value=f"{found_member.mention}", inline=True)
        log_embed.add_field(name="Ник", value=nickname, inline=True)
        log_embed.add_field(name="Срок", value=f"{days} дней" if days > 0 else "♾ Бессрочно", inline=True)
        await send_log(guild, log_embed)
    else:
        await interaction.followup.send("❌ Ошибка API", ephemeral=True)


@tree.command(name="subs", description="Список всех подписчиков")
async def cmd_subs(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только админы.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    result = await api_request("GET", "/api/subscribers")
    subs = result.get("subscribers", [])
    total = result.get("total", 0)

    if total == 0:
        await interaction.followup.send("📋 Подписчиков нет.", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 Подписчики ({total})", color=0xa94cef)

    lines = []
    for s in subs[:25]:
        status = "✅" if s.get("active") else "❌"
        hwid = "🔗" if s.get("hwid") else "—"
        exp = format_remaining(s.get("expires", 0))
        name = s.get("username") or s.get("discord_id")
        lines.append(f"{status} **{name}** — {exp} | HWID: {hwid}")

    embed.description = "\n".join(lines)
    if total > 25:
        embed.set_footer(text=f"Показаны первые 25 из {total}")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── MODERATION COMMANDS ──────────────────────────────────────
@tree.command(name="warn", description="Выдать предупреждение")
@app_commands.describe(user="Пользователь", reason="Причина")
async def cmd_warn(interaction: discord.Interaction, user: discord.Member, reason: str = "Без причины"):
    if not is_mod(interaction.user):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    if is_mod(user):
        await interaction.response.send_message("❌ Нельзя варнить модератора.", ephemeral=True)
        return

    total = add_warn(user.id, reason, interaction.user.id)

    embed = discord.Embed(title="⚠️ Предупреждение", color=0xff9900)
    embed.add_field(name="Пользователь", value=user.mention, inline=True)
    embed.add_field(name="Варнов", value=f"{total}/{WARN_BAN_THRESHOLD}", inline=True)
    embed.add_field(name="Причина", value=reason, inline=False)
    embed.set_footer(text=f"Модератор: {interaction.user}")
    await interaction.response.send_message(embed=embed)

    await log_action(interaction.guild, "Варн", interaction.user, user, reason=reason)

    # Auto-escalate
    if total >= WARN_BAN_THRESHOLD:
        try:
            await user.ban(reason=f"Достигнут лимит варнов ({total})")
            escalate_embed = discord.Embed(
                title="🔨 Автобан",
                description=f"{user.mention} забанен — {total} варнов",
                color=0xff0000,
            )
            await interaction.followup.send(embed=escalate_embed)
            await log_action(interaction.guild, "Автобан", interaction.user, user,
                             reason=f"Лимит варнов ({total})", color=0xff0000)
        except Exception:
            pass
    elif total >= WARN_MUTE_THRESHOLD:
        try:
            until = discord.utils.utcnow() + datetime.timedelta(minutes=AUTO_MUTE_MINUTES)
            await user.timeout(until, reason=f"Достигнут лимит варнов ({total})")
            escalate_embed = discord.Embed(
                title="🔇 Автомут",
                description=f"{user.mention} замучен на {AUTO_MUTE_MINUTES} мин — {total} варнов",
                color=0xff6600,
            )
            await interaction.followup.send(embed=escalate_embed)
            await log_action(interaction.guild, "Автомут", interaction.user, user,
                             reason=f"Лимит варнов ({total})", duration=f"{AUTO_MUTE_MINUTES} мин", color=0xff6600)
        except Exception:
            pass


@tree.command(name="mute", description="Замутить пользователя")
@app_commands.describe(user="Пользователь", minutes="Минуты (по умолчанию 30)", reason="Причина")
async def cmd_mute(interaction: discord.Interaction, user: discord.Member, minutes: int = 30, reason: str = "Без причины"):
    if not is_mod(interaction.user):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    if is_mod(user):
        await interaction.response.send_message("❌ Нельзя мутить модератора.", ephemeral=True)
        return

    try:
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        await user.timeout(until, reason=reason)

        embed = discord.Embed(title="🔇 Мут", color=0xff6600)
        embed.add_field(name="Пользователь", value=user.mention, inline=True)
        embed.add_field(name="Длительность", value=f"{minutes} мин", inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)

        await log_action(interaction.guild, "Мут", interaction.user, user,
                         reason=reason, duration=f"{minutes} мин", color=0xff6600)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не могу замутить этого пользователя.", ephemeral=True)


@tree.command(name="unmute", description="Размутить пользователя")
@app_commands.describe(user="Пользователь")
async def cmd_unmute(interaction: discord.Interaction, user: discord.Member):
    if not is_mod(interaction.user):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    try:
        await user.timeout(None, reason=f"Размучен {interaction.user}")

        embed = discord.Embed(
            title="🔊 Размучен",
            description=f"{user.mention} может снова писать",
            color=0x57f287,
        )
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)

        await log_action(interaction.guild, "Размут", interaction.user, user, color=0x57f287)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не могу размутить.", ephemeral=True)


@tree.command(name="kick", description="Кикнуть пользователя")
@app_commands.describe(user="Пользователь", reason="Причина")
async def cmd_kick(interaction: discord.Interaction, user: discord.Member, reason: str = "Без причины"):
    if not is_mod(interaction.user):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    if is_mod(user):
        await interaction.response.send_message("❌ Нельзя кикнуть модератора.", ephemeral=True)
        return

    try:
        await user.kick(reason=reason)

        embed = discord.Embed(title="👢 Кик", color=0xff5555)
        embed.add_field(name="Пользователь", value=f"{user} (`{user.id}`)", inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)

        await log_action(interaction.guild, "Кик", interaction.user, user, reason=reason, color=0xff5555)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не могу кикнуть.", ephemeral=True)


@tree.command(name="ban", description="Забанить пользователя")
@app_commands.describe(user="Пользователь", reason="Причина")
async def cmd_ban(interaction: discord.Interaction, user: discord.Member, reason: str = "Без причины"):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только админы.", ephemeral=True)
        return

    if is_mod(user):
        await interaction.response.send_message("❌ Нельзя банить модератора.", ephemeral=True)
        return

    try:
        await user.ban(reason=reason, delete_message_days=1)

        embed = discord.Embed(title="🔨 Бан", color=0xff0000)
        embed.add_field(name="Пользователь", value=f"{user} (`{user.id}`)", inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)

        await log_action(interaction.guild, "Бан", interaction.user, user, reason=reason, color=0xff0000)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Не могу забанить.", ephemeral=True)


@tree.command(name="warns", description="Посмотреть предупреждения")
@app_commands.describe(user="Пользователь")
async def cmd_warns(interaction: discord.Interaction, user: discord.Member):
    user_warns = get_warns(user.id)

    if not user_warns:
        embed = discord.Embed(
            title=f"📋 Варны — {user}",
            description="Нет предупреждений",
            color=0x57f287,
        )
    else:
        embed = discord.Embed(
            title=f"📋 Варны — {user} ({len(user_warns)}/{WARN_BAN_THRESHOLD})",
            color=0xff9900,
        )
        for i, w in enumerate(user_warns[-10:], 1):
            time_str = w.get("time", "?")[:16].replace("T", " ")
            embed.add_field(
                name=f"#{i} — {time_str}",
                value=w.get("reason", "Без причины"),
                inline=False,
            )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="clearwarns", description="Очистить все предупреждения")
@app_commands.describe(user="Пользователь")
async def cmd_clearwarns(interaction: discord.Interaction, user: discord.Member):
    if not is_mod(interaction.user):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    old_count = len(get_warns(user.id))
    clear_warns(user.id)

    embed = discord.Embed(
        title="🧹 Варны очищены",
        description=f"{user.mention} — удалено {old_count} предупреждений",
        color=0x57f287,
    )
    embed.set_footer(text=f"Модератор: {interaction.user}")
    await interaction.response.send_message(embed=embed)

    await log_action(interaction.guild, "Очистка варнов", interaction.user, user,
                     reason=f"Удалено {old_count} варнов", color=0x57f287)


# ─── UPDATE COMMAND ───────────────────────────────────────────
@tree.command(name="update", description="Загрузить новую версию PRIME Client (.jar)")
@app_commands.describe(
    version="Версия (например 1.1.0)",
    changelog="Что нового (список изменений)",
)
async def cmd_update(
    interaction: discord.Interaction,
    version: str = "",
    changelog: str = "",
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Только для админов.", ephemeral=True)
        return

    # Check for attached .jar
    if not interaction.data or "resolved" not in interaction.data:
        await interaction.response.send_message(
            "❌ Прикрепи .jar файл к сообщению. Используй:\n"
            "`/update version:1.1.0 changelog:Что нового` + прикрепи файл",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    # Try to get attachment from message
    # Slash commands don't natively support file attachments well,
    # so we also handle it via on_message in the update channel
    await interaction.followup.send(
        "⚠️ Slash-команды не поддерживают вложения напрямую.\n"
        f"**Кинь .jar файл в канал <#{UPDATE_CHANNEL_ID}>** с текстом:\n"
        f"```\nВерсия: {version or '1.0.0'}\n{changelog or 'Описание изменений'}\n```\n"
        "Бот сам подхватит файл и загрузит обновление!",
        ephemeral=True,
    )


async def notify_subscribers_about_update(guild: discord.Guild, version: str, changelog: str):
    """Send update notification to all personal subscriber channels."""
    try:
        channels = await guild.fetch_channels()
    except Exception:
        channels = guild.channels or []

    # Find the clients category
    personal_cat = None
    for ch in channels:
        if isinstance(ch, discord.CategoryChannel) and "клиент" in ch.name.lower():
            personal_cat = ch
            break
    if personal_cat is None:
        return

    # Find all personal channels in the category
    notified = 0
    for ch in channels:
        if isinstance(ch, discord.TextChannel) and hasattr(ch, 'category_id') and ch.category_id == personal_cat.id:
            try:
                embed = discord.Embed(
                    title="🔄 Новое обновление PRIME Client!",
                    description=f"Версия **{version}** доступна.\nПерезапусти лаунчер — обновление скачается автоматически.",
                    color=0x9b59b6,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
                if changelog:
                    cl_lines = changelog.split("\n")
                    formatted = []
                    for line in cl_lines:
                        line = line.strip()
                        if line:
                            if not line.startswith("- ") and not line.startswith("• "):
                                line = "• " + line
                            formatted.append(line)
                    if formatted:
                        embed.add_field(name="📋 Что нового", value="\n".join(formatted), inline=False)
                await ch.send(embed=embed)
                notified += 1
            except Exception as e:
                print(f"[UPDATE-NOTIFY] Failed to notify {ch.name}: {e}")
    print(f"[UPDATE-NOTIFY] Notified {notified} personal channels about v{version}")


async def process_jar_update(message: discord.Message, attachment: discord.Attachment, text: str):
    """Download .jar from Discord, upload to API, post changelog embed."""
    # Parse version and changelog from message text
    version = ""
    changelog = text.strip() if text.strip() else ""

    lines = text.strip().split("\n")
    for line in lines:
        low = line.lower().strip()
        if low.startswith("версия:") or low.startswith("version:"):
            version = line.split(":", 1)[1].strip()
            lines.remove(line)
            break

    if not version:
        # Auto-increment: get current version from API
        try:
            current = await api_request("GET", "/api/mod/version")
            old_ver = current.get("version", "1.0.0")
            parts = old_ver.split(".")
            parts[-1] = str(int(parts[-1]) + 1)
            version = ".".join(parts)
        except Exception:
            version = "1.0.1"

    changelog = "\n".join(lines).strip() if lines else changelog

    # Download .jar from Discord
    status_msg = await message.channel.send("⏳ Скачиваю файл...")
    try:
        file_bytes = await attachment.read()
    except Exception as e:
        await status_msg.edit(content=f"❌ Ошибка скачивания: {e}")
        return

    await status_msg.edit(content=f"⏳ Загружаю на сервер ({len(file_bytes) // 1024 // 1024} МБ)...")

    # Upload to API — normalize filename to PRIME-{version}.jar
    normalized_filename = f"PRIME-{version}.jar"
    result = await api_upload_mod(file_bytes, normalized_filename, version, changelog)

    if result.get("ok"):
        await status_msg.delete()

        # Post beautiful changelog embed
        embed = discord.Embed(
            title="🔄 Обновление PRIME Client",
            color=0x9b59b6,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="📦 Версия", value=f"`{version}`", inline=True)
        embed.add_field(name="📁 Файл", value=f"`{attachment.filename}`", inline=True)
        size_mb = len(file_bytes) / 1024 / 1024
        embed.add_field(name="📏 Размер", value=f"`{size_mb:.1f} МБ`", inline=True)

        if changelog:
            # Format changelog nicely
            cl_lines = changelog.split("\n")
            formatted = []
            for line in cl_lines:
                line = line.strip()
                if line:
                    if not line.startswith("- ") and not line.startswith("• "):
                        line = "• " + line
                    formatted.append(line)
            embed.add_field(
                name="📋 Что нового",
                value="\n".join(formatted) if formatted else "Без описания",
                inline=False,
            )

        embed.set_footer(text=f"Загрузил: {message.author}", icon_url=message.author.display_avatar.url)

        await message.channel.send(embed=embed)

        # Also notify in log channel
        guild = message.guild
        if guild:
            log_ch = guild.get_channel(LOG_CHANNEL_ID)
            if log_ch:
                log_embed = discord.Embed(
                    title="📦 Мод обновлён",
                    description=f"Версия `{version}` загружена {message.author.mention}",
                    color=0x2ecc71,
                )
                await log_ch.send(embed=log_embed)

        # Notify all subscribers in personal channels
        if guild:
            await notify_subscribers_about_update(guild, version, changelog)

        # Delete the original message with .jar to keep channel clean
        try:
            await message.delete()
        except Exception:
            pass
    else:
        error = result.get("error", result.get("detail", "Неизвестная ошибка"))
        await status_msg.edit(content=f"❌ Ошибка загрузки: {error}")


def convert_gdrive_url(url: str) -> str:
    """Convert Google Drive share/view URL to direct download URL."""
    # Format: https://drive.google.com/file/d/FILE_ID/view?...
    m = re.search(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)', url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    # Format: https://drive.google.com/open?id=FILE_ID
    m = re.search(r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)', url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    # Format: https://drive.google.com/uc?id=FILE_ID (already direct, add confirm)
    m = re.search(r'drive\.google\.com/uc\?.*id=([a-zA-Z0-9_-]+)', url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    return url


async def download_from_gdrive(session: aiohttp.ClientSession, url: str) -> bytes:
    """Download file from Google Drive, handling virus scan confirmation page."""
    direct_url = convert_gdrive_url(url)
    async with session.get(direct_url, timeout=aiohttp.ClientTimeout(total=300),
                           allow_redirects=True) as resp:
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status}")
        content = await resp.read()
        # Check if we got the HTML confirmation page instead of the actual file
        if len(content) < 500000 and b'<html' in content[:1000].lower():
            # Extract the confirmation URL from the page
            text = content.decode('utf-8', errors='ignore')
            # Look for confirm=XXXX or uuid=XXXX patterns
            confirm_match = re.search(r'confirm=([a-zA-Z0-9_-]+)', text)
            uuid_match = re.search(r'uuid=([a-zA-Z0-9_-]+)', text)
            if confirm_match or uuid_match:
                params = "&confirm=t"
                if uuid_match:
                    params += f"&uuid={uuid_match.group(1)}"
                retry_url = direct_url + params
                async with session.get(retry_url, timeout=aiohttp.ClientTimeout(total=300),
                                       allow_redirects=True) as resp2:
                    if resp2.status != 200:
                        raise Exception(f"HTTP {resp2.status} on confirmation retry")
                    content = await resp2.read()
            # Still got HTML? Try one more method — download via export link
            if len(content) < 500000 and b'<html' in content[:1000].lower():
                m = re.search(r'id=([a-zA-Z0-9_-]+)', direct_url)
                if m:
                    alt_url = f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
                    async with session.get(alt_url, timeout=aiohttp.ClientTimeout(total=300),
                                           allow_redirects=True) as resp3:
                        if resp3.status == 200:
                            alt_content = await resp3.read()
                            if len(alt_content) > 500000 or b'<html' not in alt_content[:1000].lower():
                                content = alt_content
        return content


async def process_url_update(message: discord.Message, download_url: str, text: str):
    """Download .jar from a URL (supports Google Drive) and upload to API."""
    # Parse version and changelog
    version = ""
    changelog = text.strip()

    # Remove URL from text for changelog
    clean_text = re.sub(r'https?://\S+', '', text).strip()
    lines = clean_text.split("\n") if clean_text else []
    for line in lines[:]:
        low = line.lower().strip()
        if low.startswith("версия:") or low.startswith("version:"):
            version = line.split(":", 1)[1].strip()
            lines.remove(line)
            break

    if not version:
        try:
            current = await api_request("GET", "/api/mod/version")
            old_ver = current.get("version", "1.0.0")
            parts = old_ver.split(".")
            parts[-1] = str(int(parts[-1]) + 1)
            version = ".".join(parts)
        except Exception:
            version = "1.0.1"

    changelog = "\n".join(lines).strip()

    is_gdrive = "drive.google.com" in download_url or "docs.google.com" in download_url
    status_msg = await message.channel.send(
        f"⏳ Скачиваю файл {'с Google Drive' if is_gdrive else 'по ссылке'}..."
    )
    try:
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            if is_gdrive:
                file_bytes = await download_from_gdrive(session, download_url)
            else:
                async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=300),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        await status_msg.edit(content=f"❌ Ошибка скачивания: HTTP {resp.status}")
                        return
                    file_bytes = await resp.read()
    except Exception as e:
        await status_msg.edit(content=f"❌ Ошибка скачивания: {e}")
        return

    # Validate that we got a real .jar (ZIP magic bytes), not an HTML page
    if len(file_bytes) < 10000 or file_bytes[:2] != b'PK':
        preview = file_bytes[:200].decode('utf-8', errors='ignore') if file_bytes else "(пусто)"
        await status_msg.edit(
            content=f"❌ Скачанный файл не является .jar (получена HTML-страница или повреждённый файл).\n"
                    f"Попробуй прикрепить .jar напрямую к сообщению вместо ссылки."
        )
        print(f"[URL-UPDATE] Not a valid JAR. Size={len(file_bytes)}, preview: {preview[:100]}")
        return

    # Always normalize filename
    filename = f"PRIME-{version}.jar"

    await status_msg.edit(content=f"⏳ Загружаю на сервер ({len(file_bytes) // 1024 // 1024} МБ)...")

    result = await api_upload_mod(file_bytes, filename, version, changelog)

    if result.get("ok"):
        await status_msg.delete()

        embed = discord.Embed(
            title="🔄 Обновление PRIME Client",
            color=0x9b59b6,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="📦 Версия", value=f"`{version}`", inline=True)
        embed.add_field(name="📁 Файл", value=f"`{filename}`", inline=True)
        size_mb = len(file_bytes) / 1024 / 1024
        embed.add_field(name="📏 Размер", value=f"`{size_mb:.1f} МБ`", inline=True)
        embed.add_field(name="🔗 Источник", value=f"Скачано по ссылке", inline=True)

        if changelog:
            cl_lines = changelog.split("\n")
            formatted = []
            for line in cl_lines:
                line = line.strip()
                if line:
                    if not line.startswith("- ") and not line.startswith("• "):
                        line = "• " + line
                    formatted.append(line)
            embed.add_field(
                name="📋 Что нового",
                value="\n".join(formatted) if formatted else "Без описания",
                inline=False,
            )

        embed.set_footer(text=f"Загрузил: {message.author}", icon_url=message.author.display_avatar.url)
        await message.channel.send(embed=embed)

        guild = message.guild
        if guild:
            log_ch = guild.get_channel(LOG_CHANNEL_ID)
            if log_ch:
                log_embed = discord.Embed(
                    title="📦 Мод обновлён (по ссылке)",
                    description=f"Версия `{version}` загружена {message.author.mention}",
                    color=0x2ecc71,
                )
                await log_ch.send(embed=log_embed)

        # Notify all subscribers in personal channels
        if guild:
            await notify_subscribers_about_update(guild, version, changelog)

        try:
            await message.delete()
        except Exception:
            pass
    else:
        error = result.get("error", result.get("detail", "Неизвестная ошибка"))
        await status_msg.edit(content=f"❌ Ошибка загрузки: {error}")


# ─── EVENTS ───────────────────────────────────────────────────
@bot.event
async def on_ready():
    import sys
    global UPDATE_CHANNEL_ID
    print(f"[INIT] on_ready fired, bot user: {bot.user}", flush=True)
    guild_obj = bot.get_guild(GUILD_ID)
    if not guild_obj:
        # Try fetching guild directly
        try:
            guild_obj = await bot.fetch_guild(GUILD_ID)
            print(f"[INIT] Fetched guild: {guild_obj.name}", flush=True)
        except Exception as e:
            print(f"[INIT] ERROR: Could not find guild {GUILD_ID}: {e}", flush=True)
    if guild_obj:
        print(f"[INIT] Guild found: {guild_obj.name} (id={guild_obj.id})", flush=True)
        # Always fetch channels to ensure we have them
        try:
            channels = await guild_obj.fetch_channels()
            print(f"[INIT] Fetched {len(channels)} channels", flush=True)
        except Exception as e:
            channels = guild_obj.channels or []
            print(f"[INIT] fetch_channels failed ({e}), using cached: {len(channels)}", flush=True)
        # Find or create update channel in "Система" category
        sistema_cat = None
        for ch in channels:
            if isinstance(ch, discord.CategoryChannel) and "систем" in ch.name.lower():
                sistema_cat = ch
                break
        update_ch = None
        for ch in channels:
            if isinstance(ch, discord.TextChannel) and "обновлени" in ch.name:
                # Only count if it's in the СИСТЕМА category
                if sistema_cat and hasattr(ch, 'category_id') and ch.category_id == sistema_cat.id:
                    update_ch = ch
                    break
        print(f"[INIT] sistema_cat={sistema_cat}, update_ch={update_ch}", flush=True)
        if update_ch is None:
            # Create category "Система" if it doesn't exist
            if sistema_cat is None:
                try:
                    sistema_cat = await guild_obj.create_category("Система")
                    print(f"[INIT] Created category: Система ({sistema_cat.id})", flush=True)
                except Exception as e:
                    print(f"[INIT] Failed to create category: {e}", flush=True)
            if sistema_cat:
                overwrites = {
                    guild_obj.default_role: discord.PermissionOverwrite(send_messages=False, read_messages=True),
                    guild_obj.me: discord.PermissionOverwrite(send_messages=True, read_messages=True),
                }
                # Allow admins to send files
                try:
                    members = guild_obj.members or []
                    for member in members:
                        if member.guild_permissions.administrator and not member.bot:
                            overwrites[member] = discord.PermissionOverwrite(send_messages=True, attach_files=True)
                except Exception:
                    pass
                try:
                    update_ch = await guild_obj.create_text_channel(
                        UPDATE_CHANNEL_NAME, category=sistema_cat, overwrites=overwrites,
                        topic="Обновления PRIME Client. Кидайте .jar сюда — бот загрузит автоматически"
                    )
                    print(f"[INIT] Created update channel: {update_ch.id}", flush=True)
                except Exception as e:
                    print(f"[INIT] Failed to create update channel: {e}", flush=True)
        if update_ch:
            UPDATE_CHANNEL_ID = update_ch.id
            IGNORED_CHANNELS.add(update_ch.id)
            print(f"[INIT] Update channel: {update_ch.name} ({update_ch.id})")

        # Find or create game logs channel
        global GAME_LOGS_CHANNEL_ID
        game_logs_ch = None
        for ch in channels:
            if isinstance(ch, discord.TextChannel) and "логи-и-управлени" in ch.name:
                game_logs_ch = ch
                break
        if game_logs_ch is None and sistema_cat:
            try:
                game_logs_ch = await guild_obj.create_text_channel(
                    GAME_LOGS_CHANNEL_NAME, category=sistema_cat,
                    topic="Логи из Minecraft + управление аккаунтами. Команда: !say acc:ник сообщение"
                )
                print(f"[INIT] Created game logs channel: {game_logs_ch.id}", flush=True)
            except Exception as e:
                print(f"[INIT] Failed to create game logs channel: {e}", flush=True)
        if game_logs_ch:
            GAME_LOGS_CHANNEL_ID = game_logs_ch.id
            IGNORED_CHANNELS.add(game_logs_ch.id)
            print(f"[INIT] Game logs channel: {game_logs_ch.name} ({game_logs_ch.id})")

    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"✅ Bot ready: {bot.user}")
    print(f"   Guild: {GUILD_ID}")
    print(f"   Commands synced")

    # Start game logs polling loop
    bot.loop.create_task(poll_game_logs())


# ─── GAME LOGS POLLING ────────────────────────────────────────

LOG_TYPE_EMOJI = {
    "chat": "💬",
    "chat_out": "📤",
    "kill": "⚔️",
    "death": "💀",
    "coords": "📍",
    "connect": "🟢",
    "disconnect": "🔴",
    "command": "⌨️",
    "item": "🎒",
    "join_server": "🌐",
    "leave_server": "👋",
}


async def poll_game_logs():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if GAME_LOGS_CHANNEL_ID:
                guild_obj = bot.get_guild(GUILD_ID)
                channel = guild_obj.get_channel(GAME_LOGS_CHANNEL_ID) if guild_obj else None
                if channel:
                    data = await api_request("GET", "/api/logs/poll")
                    if data and "logs" in data:
                        for log_entry in data["logs"]:
                            emoji = LOG_TYPE_EMOJI.get(log_entry.get("type", ""), "📋")
                            log_type = log_entry.get("type", "unknown")
                            player = log_entry.get("player", "?")
                            server = log_entry.get("server", "")
                            msg_text = log_entry.get("message", "")
                            extra = log_entry.get("extra", {})

                            embed = discord.Embed(color=0x5865F2)

                            if log_type == "chat":
                                embed.description = f"{emoji} **[ЧАТ]** `{player}` на `{server}`\n```{msg_text}```"
                            elif log_type == "chat_out":
                                embed.description = f"{emoji} **[ОТПРАВЛЕНО]** `{player}` на `{server}`\n```{msg_text}```"
                                embed.color = 0x00ff88
                            elif log_type == "kill":
                                embed.description = f"{emoji} **[УБИЙСТВО]** `{player}` убил `{msg_text}` на `{server}`"
                                embed.color = 0xff4444
                            elif log_type == "death":
                                embed.description = f"{emoji} **[СМЕРТЬ]** `{player}` на `{server}`\n{msg_text}"
                                embed.color = 0xff0000
                            elif log_type == "coords":
                                x = extra.get("x", "?")
                                y = extra.get("y", "?")
                                z = extra.get("z", "?")
                                dim = extra.get("dimension", "")
                                embed.description = f"{emoji} **[КООРДИНАТЫ]** `{player}` на `{server}`\n**X:** {x} **Y:** {y} **Z:** {z} {dim}"
                                embed.color = 0xffaa00
                            elif log_type in ("connect", "join_server"):
                                embed.description = f"{emoji} **[ПОДКЛЮЧЕНИЕ]** `{player}` → `{server}`"
                                embed.color = 0x00ff00
                            elif log_type in ("disconnect", "leave_server"):
                                embed.description = f"{emoji} **[ОТКЛЮЧЕНИЕ]** `{player}` ← `{server}`"
                                embed.color = 0xff5555
                            elif log_type == "command":
                                embed.description = f"{emoji} **[КОМАНДА]** `{player}` на `{server}`\n```{msg_text}```"
                                embed.color = 0x9b59b6
                            else:
                                embed.description = f"{emoji} **[{log_type.upper()}]** `{player}` на `{server}`\n{msg_text}"

                            ts = log_entry.get("timestamp", 0)
                            if ts:
                                embed.timestamp = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)

                            try:
                                await channel.send(embed=embed)
                            except Exception as e:
                                print(f"[GAME LOGS] Send error: {e}")
        except Exception as e:
            print(f"[GAME LOGS] Poll error: {e}")
        await asyncio.sleep(3)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild or message.guild.id != GUILD_ID:
        return

    # Handle !say command in game logs channel
    if GAME_LOGS_CHANNEL_ID and message.channel.id == GAME_LOGS_CHANNEL_ID:
        content = message.content.strip()
        # Format: !say acc:nickname message
        if content.startswith("!say "):
            parts = content[5:].strip()
            if parts.startswith("acc:"):
                rest = parts[4:]
                space_idx = rest.find(" ")
                if space_idx > 0:
                    account = rest[:space_idx].strip()
                    say_msg = rest[space_idx + 1:].strip()
                    if account and say_msg:
                        result = await api_request("POST", "/api/logs/say", {"account": account, "message": say_msg})
                        if result and result.get("ok"):
                            await message.reply(f"✅ Команда отправлена → `{account}`: `{say_msg}`", delete_after=10)
                        else:
                            await message.reply("❌ Ошибка отправки команды", delete_after=10)
                        return
            await message.reply("❌ Формат: `!say acc:ник сообщение`", delete_after=10)
            return
        return

    # Auto-pickup .jar in update channel
    if UPDATE_CHANNEL_ID and message.channel.id == UPDATE_CHANNEL_ID:
        if not message.author.guild_permissions.administrator:
            await message.reply("❌ Только админы могут загружать обновления.", delete_after=10)
            try:
                await message.delete()
            except Exception:
                pass
            return

        # Option 1: .jar file attached directly
        if message.attachments:
            jar_attachment = None
            for att in message.attachments:
                if att.filename.endswith(".jar"):
                    jar_attachment = att
                    break
            if jar_attachment:
                await process_jar_update(message, jar_attachment, message.content)
                return

        # Option 2: URL to .jar in the message text
        url_match = re.search(r'(https?://\S+\.jar\b)', message.content)
        if not url_match:
            # Also support generic URLs (not ending in .jar)
            url_match = re.search(r'(https?://\S+)', message.content)
        if url_match:
            download_url = url_match.group(1)
            await process_url_update(message, download_url, message.content)
            return
        return

    if message.channel.id in IGNORED_CHANNELS:
        return
    if is_mod(message.author):
        return

    content = message.content

    # Check bad words
    if check_bad_words(content):
        await automod_action(message, "Мат в чате")
        return

    # Check links
    if check_links(content):
        await automod_action(message, "Ссылки запрещены")
        return

    # Check caps
    if check_caps(content):
        await automod_action(message, "Слишком много капса")
        return

    # Check spam
    if check_spam(message.author.id):
        await automod_action(message, "Спам")
        return


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return

    now = datetime.datetime.now().timestamp()
    join_timestamps.append((now, member))

    # Clean old
    cutoff = now - RAID_WINDOW_SEC
    while join_timestamps and join_timestamps[0][0] < cutoff:
        join_timestamps.pop(0)

    # Check raid
    if len(join_timestamps) >= RAID_JOIN_LIMIT:
        embed = discord.Embed(
            title="🚨 Возможный рейд!",
            description=f"{len(join_timestamps)} пользователей зашли за {RAID_WINDOW_SEC} секунд",
            color=0xff0000,
            timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
        )
        recent = join_timestamps[-5:]
        names = ", ".join(f"{m.mention}" for _, m in recent)
        embed.add_field(name="Последние", value=names, inline=False)
        embed.set_footer(text="PRIME Anti-Raid")
        await send_log(member.guild, embed)

    # Welcome log
    embed = discord.Embed(
        title="📥 Новый участник",
        description=f"{member.mention} ({member})",
        color=0x57f287,
        timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
    )
    embed.add_field(name="ID", value=str(member.id), inline=True)
    created = member.created_at.strftime("%d.%m.%Y")
    embed.add_field(name="Аккаунт создан", value=created, inline=True)
    embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
    embed.set_footer(text="PRIME Logger")
    await send_log(member.guild, embed)


@bot.event
async def on_member_remove(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return

    embed = discord.Embed(
        title="📤 Участник вышел",
        description=f"{member} (`{member.id}`)",
        color=0xff5555,
        timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
    )
    roles = [r.name for r in member.roles if r.name != "@everyone"]
    if roles:
        embed.add_field(name="Роли", value=", ".join(roles), inline=False)
    embed.set_footer(text="PRIME Logger")
    await send_log(member.guild, embed)


# ─── CLOUD CONFIGS ─────────────────────────────────────────────

@tree.command(name="config_upload", description="☁️ Загрузить конфиг в облако", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(name="Название конфига", description="Описание конфига (необязательно)")
async def config_upload(interaction: discord.Interaction, name: str, description: str = ""):
    await interaction.response.send_message(
        "📎 Отправь файл конфига в следующем сообщении (в этом канале, в течение 60 секунд).",
        ephemeral=True
    )

    def check(m):
        return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and len(m.attachments) > 0

    try:
        msg = await bot.wait_for("message", check=check, timeout=60.0)
    except asyncio.TimeoutError:
        await interaction.followup.send("⏰ Время вышло! Попробуй снова.", ephemeral=True)
        return

    attachment = msg.attachments[0]
    file_bytes = await attachment.read()

    key = generate_config_key()
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    config_path = CONFIGS_DIR / f"{key}_{attachment.filename}"
    config_path.write_bytes(file_bytes)

    configs_db[key] = {
        "name": name,
        "description": description,
        "filename": attachment.filename,
        "stored_filename": config_path.name,
        "author_id": interaction.user.id,
        "author_name": str(interaction.user),
        "uploaded_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "downloads": 0,
        "size": len(file_bytes),
    }
    save_configs(configs_db)

    try:
        await msg.delete()
    except Exception:
        pass

    embed = discord.Embed(
        title="☁️ Конфиг загружен!",
        color=0x00ff88,
    )
    embed.add_field(name="Название", value=name, inline=True)
    embed.add_field(name="Ключ", value=f"`{key}`", inline=True)
    embed.add_field(name="Файл", value=attachment.filename, inline=True)
    if description:
        embed.add_field(name="Описание", value=description, inline=False)
    embed.set_footer(text=f"Поделись ключом {key} чтобы другие могли скачать!")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="config_get", description="☁️ Скачать конфиг по ключу", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Ключ конфига (8 символов)")
async def config_get(interaction: discord.Interaction, key: str):
    key = key.strip().upper()
    if key not in configs_db:
        await interaction.response.send_message(f"❌ Конфиг с ключом `{key}` не найден!", ephemeral=True)
        return

    cfg = configs_db[key]
    config_path = CONFIGS_DIR / cfg["stored_filename"]
    if not config_path.exists():
        await interaction.response.send_message("❌ Файл конфига не найден на сервере!", ephemeral=True)
        return

    cfg["downloads"] = cfg.get("downloads", 0) + 1
    save_configs(configs_db)

    embed = discord.Embed(
        title=f"☁️ {cfg['name']}",
        color=0x5865F2,
    )
    embed.add_field(name="Автор", value=cfg["author_name"], inline=True)
    embed.add_field(name="Ключ", value=f"`{key}`", inline=True)
    embed.add_field(name="Скачиваний", value=str(cfg["downloads"]), inline=True)
    if cfg.get("description"):
        embed.add_field(name="Описание", value=cfg["description"], inline=False)

    await interaction.response.send_message(
        embed=embed,
        file=discord.File(config_path, filename=cfg["filename"]),
        ephemeral=True
    )


@tree.command(name="config_search", description="☁️ Поиск конфигов по названию", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(query="Название или часть названия конфига")
async def config_search(interaction: discord.Interaction, query: str):
    query_lower = query.lower()
    results = []
    for key, cfg in configs_db.items():
        if (query_lower in cfg["name"].lower()
                or query_lower in cfg.get("description", "").lower()
                or query_lower == key.lower()):
            results.append((key, cfg))

    if not results:
        await interaction.response.send_message(
            f"🔍 По запросу **{query}** ничего не найдено.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"🔍 Результаты поиска: {query}",
        color=0x5865F2,
        description=f"Найдено: {len(results)} конфиг(ов)\nИспользуй `/config_get <ключ>` чтобы скачать",
    )
    for key, cfg in results[:15]:
        desc_text = cfg.get("description", "")[:80]
        embed.add_field(
            name=f"`{key}` — {cfg['name']}",
            value=f"Автор: {cfg['author_name']} | 📥 {cfg.get('downloads', 0)}" + (f"\n{desc_text}" if desc_text else ""),
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="config_list", description="☁️ Список всех облачных конфигов", guild=discord.Object(id=GUILD_ID))
async def config_list(interaction: discord.Interaction):
    if not configs_db:
        await interaction.response.send_message("☁️ Пока нет загруженных конфигов.", ephemeral=True)
        return

    sorted_configs = sorted(configs_db.items(), key=lambda x: x[1].get("downloads", 0), reverse=True)

    embed = discord.Embed(
        title="☁️ Облачные конфиги PRIME",
        color=0x5865F2,
        description=f"Всего: {len(sorted_configs)} конфиг(ов)\nИспользуй `/config_get <ключ>` чтобы скачать",
    )
    for key, cfg in sorted_configs[:20]:
        desc_text = cfg.get("description", "")[:60]
        embed.add_field(
            name=f"`{key}` — {cfg['name']}",
            value=f"Автор: {cfg['author_name']} | 📥 {cfg.get('downloads', 0)}" + (f" | {desc_text}" if desc_text else ""),
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="config_delete", description="☁️ Удалить свой конфиг", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Ключ конфига для удаления")
async def config_delete(interaction: discord.Interaction, key: str):
    key = key.strip().upper()
    if key not in configs_db:
        await interaction.response.send_message(f"❌ Конфиг `{key}` не найден!", ephemeral=True)
        return

    cfg = configs_db[key]
    if cfg["author_id"] != interaction.user.id and not is_admin(interaction):
        await interaction.response.send_message("❌ Ты можешь удалять только свои конфиги!", ephemeral=True)
        return

    config_path = CONFIGS_DIR / cfg["stored_filename"]
    if config_path.exists():
        config_path.unlink()

    del configs_db[key]
    save_configs(configs_db)

    await interaction.response.send_message(f"✅ Конфиг `{key}` ({cfg['name']}) удалён.", ephemeral=True)


@tree.command(name="my_configs", description="☁️ Мои загруженные конфиги", guild=discord.Object(id=GUILD_ID))
async def my_configs(interaction: discord.Interaction):
    user_configs = [(k, c) for k, c in configs_db.items() if c["author_id"] == interaction.user.id]
    if not user_configs:
        await interaction.response.send_message("У тебя нет загруженных конфигов.", ephemeral=True)
        return

    embed = discord.Embed(
        title="☁️ Мои конфиги",
        color=0x00ff88,
        description=f"Всего: {len(user_configs)}",
    )
    for key, cfg in user_configs:
        embed.add_field(
            name=f"`{key}` — {cfg['name']}",
            value=f"📥 {cfg.get('downloads', 0)} | {cfg['filename']}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.guild.id != GUILD_ID:
        return
    if message.author.bot:
        return
    if message.channel.id in IGNORED_CHANNELS:
        return

    embed = discord.Embed(
        title="🗑️ Сообщение удалено",
        color=0x95a5a6,
        timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
    )
    embed.add_field(name="Автор", value=f"{message.author.mention}", inline=True)
    embed.add_field(name="Канал", value=f"{message.channel.mention}", inline=True)
    content = message.content[:1000] if message.content else "(пусто)"
    embed.add_field(name="Содержание", value=content, inline=False)
    embed.set_footer(text="PRIME Logger")
    await send_log(message.guild, embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.guild.id != GUILD_ID:
        return
    if before.author.bot:
        return
    if before.content == after.content:
        return
    if before.channel.id in IGNORED_CHANNELS:
        return

    embed = discord.Embed(
        title="✏️ Сообщение изменено",
        color=0x3498db,
        timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
    )
    embed.add_field(name="Автор", value=f"{before.author.mention}", inline=True)
    embed.add_field(name="Канал", value=f"{before.channel.mention}", inline=True)
    old_content = before.content[:500] if before.content else "(пусто)"
    new_content = after.content[:500] if after.content else "(пусто)"
    embed.add_field(name="Было", value=old_content, inline=False)
    embed.add_field(name="Стало", value=new_content, inline=False)
    embed.set_footer(text="PRIME Logger")
    await send_log(before.guild, embed)


if __name__ == "__main__":
    # Patch aiohttp.TCPConnector to always use our SSL context
    _orig_tcp_init = aiohttp.TCPConnector.__init__
    def _patched_tcp_init(self, *args, **kwargs):
        if "ssl" not in kwargs or kwargs["ssl"] is None:
            kwargs["ssl"] = ssl_ctx
        _orig_tcp_init(self, *args, **kwargs)
    aiohttp.TCPConnector.__init__ = _patched_tcp_init
    print("[SSL] Patched aiohttp.TCPConnector to use certifi SSL context")

    MAX_RETRIES = 10
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            bot.run(BOT_TOKEN, reconnect=True)
            break
        except (aiohttp.ClientConnectorError, ConnectionResetError, ssl.SSLError) as e:
            print(f"[BOT] Ошибка подключения (попытка {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                wait = min(attempt * 5, 30)
                print(f"[BOT] Повтор через {wait} сек...")
                import time; time.sleep(wait)
            else:
                print("[BOT] Не удалось подключиться после всех попыток")
        except KeyboardInterrupt:
            print("\n[BOT] Остановлен пользователем")
            break
