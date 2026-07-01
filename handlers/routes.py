import json
import os
import re
import sqlite3
import time
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

router = Router()

# Глобальная переменная для бота
bot_instance = None

def init_bot(bot):
    """Инициализация бота"""
    global bot_instance
    bot_instance = bot

# Словари для управления чатом
waiting_queue = []  # Очередь ожидания пользователей [user_id, ...]
active_chats = {}   # Активные пары чатов {user_id: paired_user_id, ...}
user_info = {}      # Информация о пользователях {user_id: {"chat_id": chat_id, ...}, ...}

# Данные для поиска друзей по интересам
AVAILABLE_INTERESTS = [
    "Музыка",
    "Игры",
    "Аниме",
    "Кино",
    "Спорт",
    "Книги",
    "Мемы",
    "Путешествия",
    "Животные",
    "Программирование",
    "Рисование",
    "Мода",
    "Психология",
    "Кулинария",
    "K-pop"
]

AVAILABLE_GAMES = [
    "Minecraft",
    "Fortnite",
    "Roblox",
    "League of Legends",
    "Dota 2",
    "Counter-Strike 2",
    "Valorant",
    "PUBG",
    "Apex Legends",
    "Among Us",
    "Genshin Impact",
    "Overwatch 2",
    "GTA V",
    "Rocket League",
    "World of Warcraft"
]

user_interests = {}  # {user_id: {"chat_id": int, "interests": [str], "selecting": bool}}
friends_waiting_queue = {}  # {user_id: {"chat_id": int, "interests": [str]}}

user_games = {}  # {user_id: {"chat_id": int, "games": [str], "selecting": bool}}
teammates_waiting_queue = {}  # {user_id: {"chat_id": int, "games": [str]}}

support_waiting_queue = {}  # {user_id: {"chat_id": int, "role": str}}
user_ratings = {}  # {user_id: {"positive": int, "negative": int}}
complaint_counts = {}  # {user_id: int}
user_mutes = {}  # {user_id: {"until": int|None, "permanent": bool}}

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "filter_database.json")
MODERATION_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "moderation_log.json")
MAX_REPORTS = 10
SUPPORT_MAX_REPORTS = 5
MUTE_DURATION_SECONDS = 3600
AUTO_DELETE_VIOLATIONS = True


def _collect_bad_words(data, context: str = "general") -> list[str]:
    if isinstance(data, list):
        words = []
        for item in data:
            words.extend(_collect_bad_words(item, context))
        return words

    if isinstance(data, dict):
        words = []
        if isinstance(data.get("categories"), list):
            for category in data["categories"]:
                if not isinstance(category, dict):
                    continue

                severity = str(category.get("severity", "")).lower()
                if context == "support" or severity in {"critical", "high"}:
                    words.extend(_collect_bad_words(category.get("phrases", []), context))
            return words

        for value in data.values():
            words.extend(_collect_bad_words(value, context))
        return words

    if isinstance(data, str):
        cleaned = data.strip().lower()
        return [cleaned] if cleaned else []

    return []


def get_bad_words(context: str = "general") -> list[str]:
    if not os.path.exists(DB_PATH):
        return []

    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        words = _collect_bad_words(data, context)
        return [word for word in words if word]
    except Exception as e:
        print(f"Ошибка чтения файла {DB_PATH}: {e}")
        return []


def find_bad_words(text: str, context: str = "general") -> list[str]:
    if not text:
        return []

    normalized = re.sub(r"[^а-яa-z0-9]+", " ", text.lower()).strip()
    matched = []
    for word in get_bad_words(context):
        if re.search(rf"\b{re.escape(word)}\b", normalized):
            matched.append(word)
    return matched


def contains_bad_word(text: str, context: str = "general") -> bool:
    return bool(find_bad_words(text, context))


def load_violation_log() -> list[dict]:
    if not os.path.exists(MODERATION_LOG_PATH):
        return []

    try:
        with open(MODERATION_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_violation_log(log_entries: list[dict]) -> None:
    try:
        with open(MODERATION_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log_entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка записи лога модерации: {e}")


def log_violation(user_id: int, chat_id: int, context: str, text: str, action: str, matched_words: list[str] | None = None) -> None:
    entries = load_violation_log()
    entries.append({
        "timestamp": int(time.time()),
        "user_id": user_id,
        "chat_id": chat_id,
        "context": context,
        "action": action,
        "text": text[:200] if text else "",
        "matched_words": matched_words or [],
    })
    save_violation_log(entries)


async def delete_message_if_possible(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        pass


def get_mute_time_left(user_id: int) -> int | None:
    mute_state = user_mutes.get(user_id)
    if not mute_state:
        return None

    if mute_state.get("permanent"):
        return None

    until = mute_state.get("until")
    if not until:
        user_mutes.pop(user_id, None)
        return None

    if until <= int(time.time()):
        user_mutes.pop(user_id, None)
        return None

    return int(until - time.time())


def format_time_left(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs or not parts:
        parts.append(f"{secs} сек")
    return " ".join(parts)


def get_mute_message(user_id: int) -> str | None:
    mute_state = user_mutes.get(user_id)
    if not mute_state:
        return None

    if mute_state.get("permanent"):
        return "🚫 Вы в муте навсегда."

    remaining = get_mute_time_left(user_id)
    if remaining is None:
        return None

    return f"🚫 Вы в муте. Размут через {format_time_left(remaining)}"


def send_report_prompt(chat_id: int, offender_id: int, bot, context: str = "general"):
    if context == "support":
        prompt_text = "⚠️ В поддержке отправлено запрещённое слово.\n\nПожаловаться на этого пользователя?"
    else:
        prompt_text = "⚠️ Пользователь отправил запрещённое слово.\n\nПожаловаться на него?"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"report_user_{offender_id}:{context}")],
        [InlineKeyboardButton(text="Нет", callback_data=f"ignore_report_{offender_id}:{context}")],
    ])
    return bot.send_message(chat_id, prompt_text, reply_markup=keyboard)


def find_interlocutor(user_id: int, chat_id: int) -> dict | None:
    """
    Поиск собеседника для пользователя
    
    Args:
        user_id: ID пользователя
        chat_id: Chat ID пользователя
        
    Returns:
        dict с информацией о найденном собеседнике или None
    """
    global waiting_queue, active_chats, user_info
    
    # Сохраняем информацию о пользователе
    user_info[user_id] = {"chat_id": chat_id}
    
    # Если в очереди есть пользователи
    if waiting_queue:
        # Берем первого из очереди
        paired_user_id = waiting_queue.pop(0)
        
        # Создаем пару
        active_chats[user_id] = paired_user_id
        active_chats[paired_user_id] = user_id
        
        return {
            "found": True,
            "paired_user_id": paired_user_id
        }
    else:
        # Добавляем в очередь ожидания
        waiting_queue.append(user_id)
        return {"found": False, "waiting": True}

def get_paired_user(user_id: int) -> int | None:
    """Получить ID собеседника"""
    return active_chats.get(user_id)

def get_user_chat_id(user_id: int) -> int | None:
    """Получить chat_id пользователя"""
    if user_id in user_info:
        return user_info[user_id].get("chat_id")
    return None

def disconnect_user(user_id: int):
    """Отключить пользователя от чата"""
    global active_chats, user_info
    
    # Получаем собеседника
    paired_user = active_chats.pop(user_id, None)
    if paired_user:
        active_chats.pop(paired_user, None)
    
    # Удаляем информацию
    user_info.pop(user_id, None)

def got_main_reply_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="найти собеседника")],
            [KeyboardButton(text="найти друзей")],
            [KeyboardButton(text="найти тимейта")],
            [KeyboardButton(text="поддержка")],
        ],
        resize_keyboard=True
    )
    return keyboard

def get_chat_keyboard():
    """Клавиатура для активного чата"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="завершить чат")],
        ],
        resize_keyboard=True
    )
    return keyboard


def get_chat_feedback_keyboard(target_user_id: int | None = None):
    """Клавиатура для оценки завершённого чата"""
    if target_user_id is None:
        target_user_id = 0
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Хорошо", callback_data=f"rate_chat_good:{target_user_id}")],
        [InlineKeyboardButton(text="👎 Плохо", callback_data=f"rate_chat_bad:{target_user_id}")],
    ])


def get_friend_search_keyboard(selected: list | None = None):
    """Клавиатура для выбора интересов"""
    if selected is None:
        selected = []

    buttons = []
    row = []
    for interest in AVAILABLE_INTERESTS:
        text = f"✅ {interest}" if interest in selected else interest
        row.append(InlineKeyboardButton(text=text, callback_data=f"interest_{interest}"))
        if len(row) == 3:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="Готово", callback_data="friend_search_done")])
    buttons.append([InlineKeyboardButton(text="Остановить поиск", callback_data="friend_search_stop")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_friend_wait_keyboard():
    """Клавиатура для отмены поиска друзей"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Остановить поиск")],
        ],
        resize_keyboard=True
    )
    return keyboard


def get_teammate_search_keyboard(selected: list | None = None):
    """Клавиатура для выбора игр"""
    if selected is None:
        selected = []

    buttons = []
    row = []
    for game in AVAILABLE_GAMES:
        text = f"✅ {game}" if game in selected else game
        row.append(InlineKeyboardButton(text=text, callback_data=f"game_{game}"))
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="Готово", callback_data="teammate_search_done")])
    buttons.append([InlineKeyboardButton(text="Остановить поиск тимейта", callback_data="teammate_search_stop")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_teammate_wait_keyboard():
    """Клавиатура для отмены поиска тимейта"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Остановить поиск тимейта")],
        ],
        resize_keyboard=True
    )
    return keyboard


def get_support_role_keyboard():
    """Клавиатура для выбора роли поддержки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💛 Нужна поддержка", callback_data="support_need")],
        [InlineKeyboardButton(text="🤝 Поддерживающий", callback_data="support_give")],
    ])


def get_support_wait_keyboard():
    """Клавиатура для отмены поиска поддержки"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Остановить поддержку")],
        ],
        resize_keyboard=True
    )
    return keyboard


def cleanup_friend_search(user_id: int):
    """Удаляем пользователя из очереди поиска друзей"""
    friends_waiting_queue.pop(user_id, None)
    user_interests.pop(user_id, None)


def cleanup_teammate_search(user_id: int):
    """Удаляем пользователя из очереди поиска тимейта"""
    teammates_waiting_queue.pop(user_id, None)
    user_games.pop(user_id, None)


def cleanup_support_search(user_id: int):
    """Удаляем пользователя из очереди поддержки"""
    support_waiting_queue.pop(user_id, None)


def find_friend_by_interests(user_id: int, chat_id: int, interests: list) -> dict:
    """Ищем друга по совпадающим интересам"""
    global friends_waiting_queue, active_chats, user_info

    if user_id in friends_waiting_queue:
        friends_waiting_queue.pop(user_id, None)

    best_match = None
    best_matches = 0

    for waiting_user_id, info in friends_waiting_queue.items():
        waiting_interests = info.get("interests", [])
        matches = len(set(interests) & set(waiting_interests))
        if matches > best_matches:
            best_matches = matches
            best_match = waiting_user_id

    if best_match and best_matches > 0:
        paired_user_id = best_match
        paired_chat_id = friends_waiting_queue[paired_user_id]["chat_id"]
        paired_interests = friends_waiting_queue[paired_user_id]["interests"]

        del friends_waiting_queue[paired_user_id]

        user_info[user_id] = {"chat_id": chat_id, "interests": interests}
        user_info[paired_user_id] = {"chat_id": paired_chat_id, "interests": paired_interests}

        active_chats[user_id] = paired_user_id
        active_chats[paired_user_id] = user_id

        common_interests = list(set(interests) & set(paired_interests))
        return {"found": True, "paired_user_id": paired_user_id, "common_interests": common_interests}

    friends_waiting_queue[user_id] = {"chat_id": chat_id, "interests": interests}
    return {"found": False, "waiting": True}


def find_teammate_by_games(user_id: int, chat_id: int, games: list) -> dict:
    """Ищем тимейта по совпадающим играм"""
    if user_id in teammates_waiting_queue:
        teammates_waiting_queue.pop(user_id, None)

    best_match = None
    best_matches = 0

    for waiting_user_id, info in teammates_waiting_queue.items():
        waiting_games = info.get("games", [])
        matches = len(set(games) & set(waiting_games))
        if matches > best_matches:
            best_matches = matches
            best_match = waiting_user_id

    if best_match and best_matches > 0:
        paired_user_id = best_match
        paired_chat_id = teammates_waiting_queue[paired_user_id]["chat_id"]
        paired_games = teammates_waiting_queue[paired_user_id]["games"]

        del teammates_waiting_queue[paired_user_id]

        user_info[user_id] = {"chat_id": chat_id, "games": games}
        user_info[paired_user_id] = {"chat_id": paired_chat_id, "games": paired_games}

        active_chats[user_id] = paired_user_id
        active_chats[paired_user_id] = user_id

        common_games = list(set(games) & set(paired_games))
        return {"found": True, "paired_user_id": paired_user_id, "common_games": common_games}

    teammates_waiting_queue[user_id] = {"chat_id": chat_id, "games": games}
    return {"found": False, "waiting": True}


@router.message(F.text.regexp(r"(?i)^\s*поддержка\s*$"))
async def support_start(message: Message):
    """Начало поиска поддержки"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    if user_id in active_chats:
        await message.answer("❌ Вы уже в чате! Напишите /exit чтобы выйти из текущего чата")
        return

    cleanup_friend_search(user_id)
    cleanup_teammate_search(user_id)
    cleanup_support_search(user_id)

    await message.answer(
        "💛 Раздел поддержки\n\n"
        "Если тебе нужна поддержка — выбери «Нужна поддержка».\n"
        "Если ты хочешь помочь другим — выбери «Поддерживающий».\n\n"
        "Мы постараемся соединить вас быстро и спокойно.",
        reply_markup=get_support_role_keyboard()
    )


@router.message(F.text.regexp(r"(?i)^\s*найти\s+друзей\s*$"))
async def find_friends_start(message: Message):
    """Начало поиска друзей по интересам"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    if user_id in active_chats:
        await message.answer("❌ Вы уже в чате! Напишите /exit чтобы выйти из текущего чата")
        return

    if user_id in friends_waiting_queue:
        await message.answer("❌ Вы уже в очереди поиска друзей!", reply_markup=get_friend_wait_keyboard())
        return

    if user_id in waiting_queue:
        waiting_queue.remove(user_id)

    cleanup_friend_search(user_id)
    cleanup_teammate_search(user_id)
    user_interests[user_id] = {"chat_id": chat_id, "interests": [], "selecting": True}

    await message.answer(
        "🎯 Выберите один или несколько интересов:\n\n"
        "Нажимайте на кнопки, чтобы выбрать или снять выбор.\n"
        "После выбора нажмите 'Готово'.",
        reply_markup=get_friend_search_keyboard([])
    )


@router.message(F.text.regexp(r"(?i)^\s*найти\s+тимейта\s*$"))
async def find_teammate_start(message: Message):
    """Начало поиска тимейта по играм"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    if user_id in active_chats:
        await message.answer("❌ Вы уже в чате! Напишите /exit чтобы выйти из текущего чата")
        return

    if user_id in teammates_waiting_queue:
        await message.answer("❌ Вы уже в очереди поиска тимейта!", reply_markup=get_teammate_wait_keyboard())
        return

    if user_id in waiting_queue:
        waiting_queue.remove(user_id)

    cleanup_teammate_search(user_id)
    cleanup_friend_search(user_id)
    user_games[user_id] = {"chat_id": chat_id, "games": [], "selecting": True}

    await message.answer(
        "🎮 Выберите одну или несколько игр:\n\n"
        "Нажимайте на кнопки, чтобы выбрать или снять выбор.\n"
        "После выбора нажмите 'Готово'.",
        reply_markup=get_teammate_search_keyboard([])
    )


@router.message(F.text.regexp(r"(?i)^\s*остановить\s+поиск\s*$"))
async def stop_friend_search(message: Message):
    """Остановить поиск друзей и выйти из очереди"""
    user_id = message.from_user.id

    cleanup_friend_search(user_id)

    await message.answer(
        "❌ Поиск друзей остановлен.\n\nНажмите 'найти друзей' чтобы начать снова.",
        reply_markup=got_main_reply_keyboard()
    )


@router.message(F.text.regexp(r"(?i)^\s*остановить\s+поиск\s+тимейта\s*$"))
async def stop_teammate_search(message: Message):
    """Остановить поиск тимейта и выйти из очереди"""
    user_id = message.from_user.id

    cleanup_teammate_search(user_id)

    await message.answer(
        "❌ Поиск тимейта остановлен.\n\nНажмите 'найти тимейта' чтобы начать снова.",
        reply_markup=got_main_reply_keyboard()
    )


@router.message(F.text.regexp(r"(?i)^\s*остановить\s+поддержку\s*$"))
async def stop_support_search(message: Message):
    """Остановить поиск поддержки и выйти из очереди"""
    user_id = message.from_user.id

    cleanup_support_search(user_id)

    await message.answer(
        "❌ Поиск поддержки остановлен.\n\nНажмите 'поддержка', чтобы начать снова.",
        reply_markup=got_main_reply_keyboard()
    )


@router.callback_query(F.data.startswith("interest_"))
async def handle_interest_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_interests or not user_interests[user_id].get("selecting"):
        await callback.answer("❌ Сеанс выбора завершен", show_alert=True)
        return

    interest = callback.data.replace("interest_", "")
    current = user_interests[user_id]["interests"]

    if interest in current:
        current.remove(interest)
        await callback.answer(f"❌ {interest} удален")
    else:
        current.append(interest)
        await callback.answer(f"✅ {interest} добавлен")

    await callback.message.edit_reply_markup(reply_markup=get_friend_search_keyboard(current))


@router.callback_query(F.data.startswith("game_"))
async def handle_game_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_games or not user_games[user_id].get("selecting"):
        await callback.answer("❌ Сеанс выбора завершен", show_alert=True)
        return

    game = callback.data.replace("game_", "")
    current = user_games[user_id]["games"]

    if game in current:
        current.remove(game)
        await callback.answer(f"❌ {game} удалена")
    else:
        current.append(game)
        await callback.answer(f"✅ {game} добавлена")

    await callback.message.edit_reply_markup(reply_markup=get_teammate_search_keyboard(current))


@router.callback_query(F.data == "support_need")
async def handle_support_need(callback: CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if user_id in active_chats:
        await callback.answer("❌ Вы уже в чате", show_alert=True)
        return

    cleanup_support_search(user_id)

    for waiting_user_id, info in list(support_waiting_queue.items()):
        if info.get("role") == "supporter":
            paired_user_id = waiting_user_id
            paired_chat_id = info.get("chat_id")
            cleanup_support_search(paired_user_id)

            user_info[user_id] = {"chat_id": chat_id, "support_role": "need_support"}
            user_info[paired_user_id] = {"chat_id": paired_chat_id, "support_role": "supporter"}
            active_chats[user_id] = paired_user_id
            active_chats[paired_user_id] = user_id

            if callback.message:
                try:
                    await callback.message.delete()
                except Exception:
                    pass

            await bot_instance.send_message(
                chat_id,
                "💛 Ты подключён к поддержке.\n\n"
                "Сейчас рядом есть человек, который готов тебя выслушать.\n"
                "Пиши спокойно, мы тут поддержим.",
                reply_markup=get_chat_keyboard()
            )

            if paired_chat_id:
                try:
                    await bot_instance.send_message(
                        paired_chat_id,
                        "💛 Ты подключён к поддержке.\n\n"
                        "Сейчас рядом человек, который нуждается в поддержке.\n"
                        "Будь рядом, выслушай и помоги, если можешь.",
                        reply_markup=get_chat_keyboard()
                    )
                except Exception as e:
                    print(f"Ошибка при уведомлении поддержки: {e}")
            await callback.answer()
            return

    support_waiting_queue[user_id] = {"chat_id": chat_id, "role": "need_support"}

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    await bot_instance.send_message(
        chat_id,
        "⏳ Ты в очереди поддержки.\n\n"
        "Мы скоро подберём человека, который сможет поддержать тебя.",
        reply_markup=get_support_wait_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "support_give")
async def handle_support_give(callback: CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if user_id in active_chats:
        await callback.answer("❌ Вы уже в чате", show_alert=True)
        return

    cleanup_support_search(user_id)

    for waiting_user_id, info in list(support_waiting_queue.items()):
        if info.get("role") == "need_support":
            paired_user_id = waiting_user_id
            paired_chat_id = info.get("chat_id")
            cleanup_support_search(paired_user_id)

            user_info[user_id] = {"chat_id": chat_id, "support_role": "supporter"}
            user_info[paired_user_id] = {"chat_id": paired_chat_id, "support_role": "need_support"}
            active_chats[user_id] = paired_user_id
            active_chats[paired_user_id] = user_id

            if callback.message:
                try:
                    await callback.message.delete()
                except Exception:
                    pass

            await bot_instance.send_message(
                chat_id,
                "💛 Ты подключён как поддерживающий.\n\n"
                "Рядом человек, которому нужна поддержка.\n"
                "Постарайся быть тёплым и спокойным.",
                reply_markup=get_chat_keyboard()
            )

            if paired_chat_id:
                try:
                    await bot_instance.send_message(
                        paired_chat_id,
                        "💛 Ты подключён к поддержке.\n\n"
                        "Сейчас рядом есть человек, который готов тебя выслушать.\n"
                        "Пиши спокойно, мы тут поддержим.",
                        reply_markup=get_chat_keyboard()
                    )
                except Exception as e:
                    print(f"Ошибка при уведомлении поддержки: {e}")
            await callback.answer()
            return

    support_waiting_queue[user_id] = {"chat_id": chat_id, "role": "supporter"}

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    await bot_instance.send_message(
        chat_id,
        "🤝 Ты в очереди поддерживающих.\n\n"
        "Как только кто-то будет нуждаться в поддержке, мы сразу соединёнм вас.",
        reply_markup=get_support_wait_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "support_stop")
async def handle_support_stop(callback: CallbackQuery):
    user_id = callback.from_user.id
    cleanup_support_search(user_id)

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    await bot_instance.send_message(
        callback.message.chat.id,
        "❌ Поиск поддержки остановлен.\n\nНажмите 'поддержка', чтобы начать снова.",
        reply_markup=got_main_reply_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate_chat_good"))
async def handle_rate_chat_good(callback: CallbackQuery):
    target_user_id = callback.from_user.id
    if ":" in callback.data:
        try:
            target_user_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            target_user_id = callback.from_user.id

    ratings = user_ratings.setdefault(target_user_id, {"positive": 0, "negative": 0})
    ratings["positive"] += 1

    if callback.message:
        try:
            await callback.message.edit_text(
                "Спасибо за оценку! 👍\n\n"
                f"Текущий рейтинг: 👍 {ratings['positive']} | 👎 {ratings['negative']}",
                reply_markup=None
            )
        except Exception:
            pass

    await callback.answer("Спасибо за отзыв!", show_alert=False)


@router.callback_query(F.data.startswith("rate_chat_bad"))
async def handle_rate_chat_bad(callback: CallbackQuery):
    target_user_id = callback.from_user.id
    if ":" in callback.data:
        try:
            target_user_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            target_user_id = callback.from_user.id

    ratings = user_ratings.setdefault(target_user_id, {"positive": 0, "negative": 0})
    ratings["negative"] += 1

    if callback.message:
        try:
            await callback.message.edit_text(
                "Спасибо за честный отзыв. 💛\n\n"
                f"Текущий рейтинг: 👍 {ratings['positive']} | 👎 {ratings['negative']}",
                reply_markup=None
            )
        except Exception:
            pass

    await callback.answer("Спасибо за отзыв!", show_alert=False)


@router.callback_query(F.data.startswith("report_user_"))
async def handle_report_user(callback: CallbackQuery):
    raw_data = callback.data.replace("report_user_", "")
    parts = raw_data.split(":", 1)
    offender_id = int(parts[0])
    context = parts[1] if len(parts) > 1 else "general"
    threshold = SUPPORT_MAX_REPORTS if context == "support" else MAX_REPORTS

    counts = complaint_counts.setdefault(offender_id, 0) + 1
    complaint_counts[offender_id] = counts

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    if user_mutes.get(offender_id, {}).get("permanent"):
        log_violation(offender_id, callback.message.chat.id if callback.message else 0, context, "повторная жалоба на уже замученного пользователя", "already_muted")
        await callback.answer("⚠️ Пользователь уже находится в вечном муте.", show_alert=True)
        return

    if counts > threshold:
        user_mutes[offender_id] = {"until": None, "permanent": True}
        log_violation(offender_id, callback.message.chat.id if callback.message else 0, context, "много жалоб", "mute_permanent")
        await bot_instance.send_message(
            offender_id,
            "🚫 Вы отправили слишком много нарушающих сообщений и были отправлены в вечный мут."
        )
        await callback.answer("⚠️ Жалоба принята. Пользователь отправлен в вечный мут.", show_alert=True)
        return

    if counts >= threshold:
        user_mutes[offender_id] = {"until": int(time.time()) + MUTE_DURATION_SECONDS, "permanent": False}
        log_violation(offender_id, callback.message.chat.id if callback.message else 0, context, "много жалоб", "mute_hour")
        await bot_instance.send_message(
            offender_id,
            f"🚫 Вы отправили слишком много нарушающих сообщений и были отправлены в мут на {format_time_left(MUTE_DURATION_SECONDS)}"
        )
        await callback.answer("⚠️ Жалоба принята. Пользователь отправлен в мут.", show_alert=True)
        return

    log_violation(offender_id, callback.message.chat.id if callback.message else 0, context, "жалоба на нарушителя", "report_received")
    await callback.answer(f"⚠️ Жалоба принята. Всего жалоб: {counts}/{threshold}", show_alert=True)


@router.callback_query(F.data.startswith("ignore_report_"))
async def handle_ignore_report(callback: CallbackQuery):
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("Жалоба проигнорирована", show_alert=False)


@router.callback_query(F.data == "friend_search_done")
async def handle_friend_search_done(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_interests:
        await callback.answer("❌ Ошибка: информация не найдена", show_alert=True)
        return

    selected = user_interests[user_id]["interests"]
    chat_id = user_interests[user_id]["chat_id"]

    if not selected:
        await callback.answer("⚠️ Выберите хотя бы один интерес", show_alert=True)
        return

    user_interests[user_id]["selecting"] = False
    cleanup_friend_search(user_id)
    result = find_friend_by_interests(user_id, chat_id, selected)

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    if result["found"]:
        paired_user_id = result["paired_user_id"]
        paired_chat_id = get_user_chat_id(paired_user_id)
        common_text = "\n".join([f"• {i}" for i in result["common_interests"]])

        await bot_instance.send_message(
            chat_id,
            f"✅ Друг найден!\n\n"
            f"Общие интереси:\n{common_text}\n\n"
            f"Вы можете начать общаться.\n"
            f"Напишите /exit чтобы завершить чат.",
            reply_markup=get_chat_keyboard()
        )

        if paired_chat_id:
            try:
                await bot_instance.send_message(
                    paired_chat_id,
                    f"✅ Друг найден!\n\n"
                    f"Общие интереси:\n{common_text}\n\n"
                    f"Вы можете начать общаться.\n"
                    f"Напишите /exit чтобы завершить чат.",
                    reply_markup=get_chat_keyboard()
                )
            except Exception as e:
                print(f"Ошибка при уведомлении друга: {e}")
    else:
        await bot_instance.send_message(
            chat_id,
            "🔍 Вы в очереди поиска друзей.\n\n"
            "Нажмите 'Остановить поиск', чтобы выйти из очереди.",
            reply_markup=get_friend_wait_keyboard()
        )

    await callback.answer()


@router.callback_query(F.data == "friend_search_stop")
async def handle_friend_search_stop(callback: CallbackQuery):
    user_id = callback.from_user.id
    cleanup_friend_search(user_id)

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    await callback.message.answer(
        "❌ Поиск друзей остановлен.\n\nНажмите 'найти друзей' чтобы начать снова.",
        reply_markup=got_main_reply_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "teammate_search_done")
async def handle_teammate_search_done(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_games:
        await callback.answer("❌ Ошибка: информация не найдена", show_alert=True)
        return

    selected = user_games[user_id]["games"]
    chat_id = user_games[user_id]["chat_id"]

    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одну игру", show_alert=True)
        return

    user_games[user_id]["selecting"] = False
    cleanup_teammate_search(user_id)
    result = find_teammate_by_games(user_id, chat_id, selected)

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    if result["found"]:
        paired_user_id = result["paired_user_id"]
        paired_chat_id = get_user_chat_id(paired_user_id)
        common_text = "\n".join([f"• {i}" for i in result["common_games"]])

        await bot_instance.send_message(
            chat_id,
            f"✅ Тимейт найден!\n\n"
            f"Общие игры:\n{common_text}\n\n"
            f"Вы можете начать общаться.\n"
            f"Напишите /exit чтобы завершить чат.",
            reply_markup=get_chat_keyboard()
        )

        if paired_chat_id:
            try:
                await bot_instance.send_message(
                    paired_chat_id,
                    f"✅ Тимейт найден!\n\n"
                    f"Общие игры:\n{common_text}\n\n"
                    f"Вы можете начать общаться.\n"
                    f"Напишите /exit чтобы завершить чат.",
                    reply_markup=get_chat_keyboard()
                )
            except Exception as e:
                print(f"Ошибка при уведомлении тимейта: {e}")
    else:
        await bot_instance.send_message(
            chat_id,
            "🔍 Вы в очереди поиска тимейта.\n\n"
            "Нажмите 'Остановить поиск тимейта', чтобы выйти из очереди.",
            reply_markup=get_teammate_wait_keyboard()
        )

    await callback.answer()


@router.callback_query(F.data == "teammate_search_stop")
async def handle_teammate_search_stop(callback: CallbackQuery):
    user_id = callback.from_user.id
    cleanup_teammate_search(user_id)

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    await callback.message.answer(
        "❌ Поиск тимейта остановлен.\n\nНажмите 'найти тимейта' чтобы начать снова.",
        reply_markup=got_main_reply_keyboard()
    )
    await callback.answer()

@router.message(Command("start"))
async def start(message: Message):
    print("start...")
    await message.answer("""👋 Добро пожаловать!

🌍 Здесь ты можешь:
💬 Общаться в анонимном чате
🤝 Находить новых друзей
🎮 Искать тиммейтов для игр
� Получать поддержку

🛡️ Мы стараемся поддерживать комфортную атмосферу, поэтому оскорбления и нежелательный контент могут привести к ограничениям.

Выбери действие в меню ниже и найди людей, с которыми действительно интересно общаться!

Напиши /search, чтобы найти собеседника.
""",
        reply_markup=got_main_reply_keyboard())

@router.message(Command("search"))
@router.message(F.text.lower() == "найти собеседника")
async def search(message: Message):
    """Поиск собеседника"""
    print("search...")
    user_id = message.from_user.id
    chat_id = message.chat.id

    cleanup_friend_search(user_id)
    
    # Проверяем, не в чате ли уже пользователь
    if user_id in active_chats:
        await message.answer("❌ Вы уже в чате! Напишите /exit чтобы выйти из текущего чата")
        return
    
    # Ищем собеседника
    result = find_interlocutor(user_id, chat_id)
    
    if result["found"]:
        # Собеседник найден!
        paired_user_id = result["paired_user_id"]
        paired_chat_id = get_user_chat_id(paired_user_id)
        
        await message.answer(
            "✅ Собеседник найден! 🎉\n\n"
            "Вы подключены к анонимному чату.\n"
            "Можете начинать общаться!\n\n"
            "Пишите /exit чтобы завершить чат.",
            reply_markup=get_chat_keyboard()
        )
        
        # Уведомляем второго собеседника
        if paired_chat_id:
            try:
                await bot_instance.send_message(
                    paired_chat_id,
                    "✅ Собеседник найден! 🎉\n\n"
                    "Вы подключены к анонимному чату.\n"
                    "Можете начинать общаться!\n\n"
                    "Пишите /exit чтобы завершить чат.",
                    reply_markup=get_chat_keyboard()
                )
            except Exception as e:
                print(f"Ошибка при отправке уведомления: {e}")
    else:
        # Собеседник не найден, добавлены в очередь
        await message.answer(
            "🔍 Собеседник не найден...\n\n"
            "Вас добавили в очередь ожидания ⏳\n"
            "Ждите, пока кто-то присоединится!\n\n"
            "Пишите /exit чтобы отменить поиск.",
            reply_markup=get_chat_keyboard()
        )

@router.message(Command("exit"))
@router.message(F.text.lower() == "завершить чат")
async def exit_chat(message: Message):
    """Выход из чата"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Проверяем, не в чате ли пользователь и не в очереди поиска
    if user_id not in active_chats and user_id not in waiting_queue and user_id not in friends_waiting_queue and user_id not in user_interests and user_id not in teammates_waiting_queue and user_id not in user_games and user_id not in support_waiting_queue:
        await message.answer("❌ Вы не в чате", reply_markup=got_main_reply_keyboard())
        return

    # Если пользователь в очереди поиска собеседника
    if user_id in waiting_queue:
        waiting_queue.remove(user_id)
        user_info.pop(user_id, None)
        await message.answer(
            "❌ Вы отменили поиск собеседника\n\n"
            "Нажмите 'найти собеседника' чтобы начать заново",
            reply_markup=got_main_reply_keyboard()
        )
        return

    # Если пользователь в поиске друзей
    if user_id in friends_waiting_queue or user_id in user_interests:
        cleanup_friend_search(user_id)
        await message.answer(
            "❌ Вы отменили поиск друзей\n\n"
            "Нажмите 'найти друзей' чтобы начать снова",
            reply_markup=got_main_reply_keyboard()
        )
        return

    # Если пользователь в поиске тимейта
    if user_id in teammates_waiting_queue or user_id in user_games:
        cleanup_teammate_search(user_id)
        await message.answer(
            "❌ Вы отменили поиск тимейта\n\n"
            "Нажмите 'найти тимейта' чтобы начать снова",
            reply_markup=got_main_reply_keyboard()
        )
        return

    # Если пользователь в поиске поддержки
    if user_id in support_waiting_queue:
        cleanup_support_search(user_id)
        await message.answer(
            "❌ Вы отменили поиск поддержки\n\n"
            "Нажмите 'поддержка' чтобы начать снова",
            reply_markup=got_main_reply_keyboard()
        )
        return
    
    # Если в активном чате
    paired_user = get_paired_user(user_id)
    if paired_user:
        paired_chat_id = get_user_chat_id(paired_user)
        
        # Удаляем из активных чатов
        disconnect_user(user_id)
        disconnect_user(paired_user)
        
        # Уведомляем первого пользователя
        await message.answer(
            "❌ Вы завершили чат\n\n"
            "Собеседник был уведомлен о выходе.",
            reply_markup=got_main_reply_keyboard()
        )
        
        # Уведомляем второго пользователя
        if paired_chat_id:
            try:
                await bot_instance.send_message(
                    paired_chat_id,
                    "❌ Собеседник завершил чат 😢\n\n"
                    "Нажмите 'найти собеседника' чтобы начать новый чат",
                    reply_markup=got_main_reply_keyboard()
                )
            except Exception as e:
                print(f"Ошибка при уведомлении собеседника: {e}")

        feedback_text = (
            "⭐ Оцените этого собеседника\n\n"
            "Было ли общение полезным и комфортным?"
        )
        await message.answer(feedback_text, reply_markup=get_chat_feedback_keyboard(paired_user))
        if paired_chat_id:
            try:
                await bot_instance.send_message(
                    paired_chat_id,
                    feedback_text,
                    reply_markup=get_chat_feedback_keyboard(user_id)
                )
            except Exception as e:
                print(f"Ошибка при отправке оценки: {e}")

@router.message()
async def forward_message(message: Message):
    """Пересылка сообщений между собеседниками"""
    user_id = message.from_user.id
    mute_message = get_mute_message(user_id)
    if mute_message:
        log_violation(user_id, message.chat.id, "support" if user_info.get(user_id, {}).get("support_role") else "general", message.text or "", "message_while_muted", [])
        await message.answer(mute_message, reply_markup=got_main_reply_keyboard())
        return

    if message.text:
        normalized = message.text.lower().strip()
        if normalized == "найти друзей":
            return await find_friends_start(message)
        if normalized == "найти тимейта":
            return await find_teammate_start(message)
        if normalized == "найти собеседника":
            return await search(message)
        if normalized == "поддержка":
            return await support_start(message)
        if normalized == "завершить чат":
            return await exit_chat(message)
        if normalized == "остановить поиск":
            return await stop_friend_search(message)
        if normalized == "остановить поиск тимейта":
            return await stop_teammate_search(message)
        if normalized == "остановить поддержку":
            return await stop_support_search(message)

    # Проверяем, в активном ли чате пользователь
    paired_user = get_paired_user(user_id)
    if not paired_user:
        await message.answer(
            "❌ Вы не в чате!\n\n"
            "Нажмите 'найти собеседника' чтобы начать чат",
            reply_markup=got_main_reply_keyboard()
        )
        return
    
    # Получаем chat_id собеседника
    paired_chat_id = get_user_chat_id(paired_user)
    if not paired_chat_id:
        await message.answer("❌ Ошибка: не удалось найти собеседника")
        return
    
    # Пересылаем оригинальное сообщение: текст — как есть, медиа — копируем
    if not bot_instance:
        await message.answer("❌ Внутренняя ошибка: бот не инициализирован")
        return

    try:
        # Текстовые сообщения отправляем как есть
        if message.text:
            context = "support" if user_info.get(user_id, {}).get("support_role") else "general"
            matched_words = find_bad_words(message.text, context=context)
            if matched_words:
                log_violation(user_id, message.chat.id, context, message.text, "bad_word_detected", matched_words)
                if AUTO_DELETE_VIOLATIONS:
                    await delete_message_if_possible(message)
                await bot_instance.send_message(chat_id=message.chat.id, text="⚠️ Обнаружено запрещённое слово")
                await bot_instance.send_message(chat_id=paired_chat_id, text=message.text)
                await send_report_prompt(paired_chat_id, user_id, bot_instance, context=context)
                return

            await bot_instance.send_message(chat_id=paired_chat_id, text=message.text)
            return

        # Для медиа и других типов сообщений используем copy_message чтобы сохранить содержимое
        await bot_instance.copy_message(chat_id=paired_chat_id, from_chat_id=message.chat.id, message_id=message.message_id)

    except Exception as e:
        print(f"Ошибка при пересылке сообщения: {e}")
        await message.answer("❌ Ошибка при отправке сообщения")

