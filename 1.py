import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Any
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import asyncio
import uuid
import socket
import httpx
from calendar import monthrange
import math
import json
import os

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен бота и ID группы
BOT_TOKEN = "8500420809:AAGicKiwQWQY-kYvbRgW8fP6gsB0lK9EIyA"
GROUP_ID = -5149803300

# Состояния для ConversationHandler
SELECTING_DATES, SELECTING_COUNT, SELECTING_TIMES = range(3)

# Доступное время для публикаций (с 7:00 до 22:00)
AVAILABLE_HOURS = list(range(7, 23))
AVAILABLE_TIMES = [f"{hour:02d}:00" for hour in AVAILABLE_HOURS]

# Файл для хранения данных
DATA_FILE = 'bot_data.json'

# ID первого администратора (ваш ID)
INITIAL_ADMIN_ID = 1070744113

# Хранилище для медиа-групп
media_groups: Dict[str, Dict] = {}

# Глобальные переменные для данных
ADMINS: Set[int] = set()
suggestions: Dict[str, Dict] = {}
scheduled_messages: Dict[str, Dict] = {}
user_sessions: Dict[int, Dict] = {}

# Загрузка данных из файла
def load_data():
    """Загрузка данных из JSON файла"""
    global ADMINS, suggestions, scheduled_messages
    
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # Загружаем администраторов
                ADMINS = set(data.get('admins', [INITIAL_ADMIN_ID]))
                
                # Загружаем предложения
                suggestions = data.get('suggestions', {})
                
                # Загружаем запланированные сообщения
                scheduled_messages = {}
                for msg_id, msg in data.get('scheduled_messages', {}).items():
                    # Восстанавливаем datetime из строки
                    if 'datetime' in msg and isinstance(msg['datetime'], str):
                        try:
                            msg['datetime'] = datetime.fromisoformat(msg['datetime'])
                        except:
                            msg['datetime'] = None
                    if 'date' in msg and isinstance(msg['date'], str):
                        try:
                            msg['date'] = datetime.fromisoformat(msg['date']).date()
                        except:
                            msg['date'] = None
                    
                    # Восстанавливаем forwarded_messages_info
                    if 'forwarded_messages_info' not in msg:
                        msg['forwarded_messages_info'] = []
                    
                    scheduled_messages[msg_id] = msg
                
                logger.info(f"Данные загружены: {len(ADMINS)} админов, {len(suggestions)} предложений, {len(scheduled_messages)} запланированных постов")
                
        except Exception as e:
            logger.error(f"Ошибка загрузки данных: {e}")
            ADMINS = {INITIAL_ADMIN_ID}
            suggestions = {}
            scheduled_messages = {}
    else:
        ADMINS = {INITIAL_ADMIN_ID}
        suggestions = {}
        scheduled_messages = {}
    
    return {
        'admins': list(ADMINS),
        'suggestions': suggestions,
        'scheduled_messages': scheduled_messages
    }

def save_data():
    """Сохранение данных в JSON файл"""
    try:
        # Подготавливаем данные для сериализации
        serializable_data = {
            'admins': list(ADMINS),
            'suggestions': suggestions,
            'scheduled_messages': {}
        }
        
        # Сериализуем запланированные сообщения
        for msg_id, msg in scheduled_messages.items():
            serializable_msg = msg.copy()
            
            # Удаляем несериализуемые объекты
            serializable_msg.pop('bot', None)
            serializable_msg.pop('original_messages', None)
            serializable_msg.pop('forwarded_messages', None)
            
            # Конвертируем datetime в строку
            if 'datetime' in serializable_msg and serializable_msg['datetime']:
                if isinstance(serializable_msg['datetime'], datetime):
                    serializable_msg['datetime'] = serializable_msg['datetime'].isoformat()
            
            if 'date' in serializable_msg and serializable_msg['date']:
                if hasattr(serializable_msg['date'], 'isoformat'):
                    serializable_msg['date'] = serializable_msg['date'].isoformat()
            
            serializable_data['scheduled_messages'][msg_id] = serializable_msg
        
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable_data, f, ensure_ascii=False, indent=2)
        
        logger.info("Данные сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")

# Инициализация планировщика
scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))

class PostScheduler:
    def __init__(self):
        self.scheduler = scheduler
        self.max_retries = 3
        self.retry_delay = 5
        
    async def send_with_retry(self, bot, method, *args, **kwargs):
        """Отправка сообщения с повторными попытками при ошибке"""
        for attempt in range(self.max_retries):
            try:
                return await method(*args, **kwargs)
            except (httpx.ReadError, httpx.ConnectError, socket.error) as e:
                logger.warning(f"Ошибка сети при отправке (попытка {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                else:
                    raise
            except Exception as e:
                logger.error(f"Неизвестная ошибка при отправке: {e}")
                raise
            
    async def send_scheduled_message(self, chat_id: int, message_data: Dict):
        """Отправка запланированного сообщения как репост"""
        try:
            bot = message_data.get('bot')
            if not bot:
                logger.error("Bot object not found in message_data")
                return
            
            user_id = message_data.get('user_id')
            
            # Получаем информацию о сообщениях для репоста
            forwarded_messages_info = message_data.get('forwarded_messages_info', [])
            
            if not forwarded_messages_info:
                logger.error("Нет информации о сообщениях для отправки")
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text="❌ Ошибка: не удалось восстановить сообщение для публикации. Пожалуйста, создайте пост заново."
                    )
                except:
                    pass
                return
            
            post_date_str = message_data.get('date')
            post_time = message_data.get('time')
            
            # Проверяем, нужно ли отправить сообщение сегодня
            today = datetime.now().date()
            if post_date_str:
                try:
                    if isinstance(post_date_str, str):
                        post_date = datetime.fromisoformat(post_date_str).date()
                    else:
                        post_date = post_date_str
                    
                    if post_date != today:
                        logger.info(f"Пропускаем пост на дату {post_date}, сегодня {today}")
                        return
                except Exception as e:
                    logger.error(f"Ошибка при парсинге даты: {e}")
            
            # Отправляем все сообщения
            for i, msg_info in enumerate(forwarded_messages_info):
                try:
                    logger.info(f"Отправка репоста {i+1}/{len(forwarded_messages_info)}: из чата {msg_info['chat_id']}, сообщение {msg_info['message_id']}")
                    
                    await self.send_with_retry(
                        bot,
                        bot.forward_message,
                        chat_id=chat_id,
                        from_chat_id=msg_info['chat_id'],
                        message_id=msg_info['message_id']
                    )
                    # Небольшая задержка между сообщениями в группе
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Ошибка при отправке репоста {i+1}: {e}")
            
            logger.info(f"{'Медиа-группа' if len(forwarded_messages_info) > 1 else 'Сообщение'} из {len(forwarded_messages_info)} сообщений отправлена в чат {chat_id}")
            
            # Уведомляем пользователя
            try:
                media_text = " (медиа-группа)" if len(forwarded_messages_info) > 1 else ""
                await self.send_with_retry(
                    bot,
                    bot.send_message,
                    chat_id=user_id,
                    text=f"✅ Ваш пост{media_text} был опубликован {today.strftime('%d.%m.%Y')} в {post_time}"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя: {e}")
            
        except Exception as e:
            logger.error(f"Критическая ошибка при отправке сообщения: {e}")

# Создание экземпляра планировщика
post_scheduler = PostScheduler()

async def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    return user_id in ADMINS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    # Игнорируем сообщения из группы
    if update.message.chat.type != 'private':
        return
    
    if user_id in user_sessions:
        del user_sessions[user_id]
    
    if context.user_data:
        context.user_data.clear()
    
    # Проверяем, является ли пользователь администратором
    if await is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton("📅 Запланировать пост", callback_data="schedule_post")],
            [InlineKeyboardButton("📋 Все запланированные посты", callback_data="my_posts_1")],
            [InlineKeyboardButton("👥 Управление администраторами", callback_data="manage_admins")],
            [InlineKeyboardButton("📨 Предложения от пользователей", callback_data="view_suggestions_1")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("📝 Предложить пост", callback_data="suggest_post")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "👋 Привет! Я бот для планирования публикаций.\n\n"
        "📢 **Для администраторов:**\n"
        "• Вы можете напрямую планировать посты\n"
        "• Управлять другими администраторами\n"
        "• Просматривать и одобрять предложения\n\n"
        "👤 **Для обычных пользователей:**\n"
        "• Вы можете предлагать посты для публикации\n"
        "• После одобрения администратора пост будет запланирован\n\n"
        "Выберите действие:"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def ignore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Игнорировать нажатие на кнопку"""
    await update.callback_query.answer()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на инлайн кнопки"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    # Игнорируем если это группа
    if query.message.chat.type != 'private':
        return
    
    if query.data == "schedule_post":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ У вас нет прав администратора для этой операции.")
            return
        await query.edit_message_text(
            "📝 Отправьте мне репост сообщения, которое хотите запланировать.\n"
            "Просто перешлите любое сообщение из канала в этот чат.\n\n"
            "✅ Поддерживаются медиа-группы (несколько фото/видео)\n"
            "✅ Сообщение будет опубликовано как репост с сохранением авторства.\n"
            "❌ Если отправить просто текст, он будет опубликован от имени бота.\n\n"
            "Для отмены используйте /cancel"
        )
        return SELECTING_DATES
    
    elif query.data == "suggest_post":
        await query.edit_message_text(
            "📝 Отправьте мне репост сообщения, которое хотите предложить для публикации.\n"
            "Просто перешлите любое сообщение из канала в этот чат.\n\n"
            "✅ Поддерживаются медиа-группы (несколько фото/видео)\n"
            "✅ После выбора дат и времени, ваше предложение будет отправлено администраторам на рассмотрение.\n\n"
            "Для отмены используйте /cancel"
        )
        return SELECTING_DATES
    
    elif query.data == "manage_admins":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ У вас нет прав для этой операции.")
            return
        await show_admin_management(query)
    
    elif query.data.startswith("view_suggestions_"):
        if not await is_admin(user_id):
            return
        page = int(query.data.split('_')[2])
        await show_suggestions(query, user_id, page)
    
    elif query.data.startswith("approve_"):
        if not await is_admin(user_id):
            return
        suggestion_id = query.data.replace("approve_", "")
        await approve_suggestion(query, user_id, suggestion_id)
    
    elif query.data.startswith("reject_"):
        if not await is_admin(user_id):
            return
        suggestion_id = query.data.replace("reject_", "")
        await reject_suggestion(query, user_id, suggestion_id)
    
    elif query.data.startswith("my_posts_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ У вас нет прав для просмотра этой информации.")
            return
        page = int(query.data.split('_')[2])
        await show_user_posts(query, user_id, page)
    
    elif query.data.startswith("next_page_"):
        if not await is_admin(user_id):
            return
        page = int(query.data.split('_')[2])
        await show_user_posts(query, user_id, page)
    
    elif query.data.startswith("prev_page_"):
        if not await is_admin(user_id):
            return
        page = int(query.data.split('_')[2])
        await show_user_posts(query, user_id, page)
    
    elif query.data == "help":
        await show_help(query)
    
    elif query.data == "back_to_menu":
        await show_main_menu(query)
    
    elif query.data.startswith("delete_"):
        if not await is_admin(user_id):
            await query.edit_message_text("❌ Только администраторы могут удалять посты.")
            return
        post_id = query.data.replace("delete_", "")
        if post_id in scheduled_messages:
            try:
                scheduler.remove_job(f"post_{post_id}")
            except:
                pass
            del scheduled_messages[post_id]
            save_data()
            await query.edit_message_text("✅ Пост успешно удален!")
            await asyncio.sleep(1)
            await show_main_menu(query)
    
    elif query.data == "finish_dates":
        if user_id in user_sessions:
            if user_sessions[user_id].get('selected_dates'):
                await show_count_selection(query, user_id)
                return SELECTING_COUNT
            else:
                await query.edit_message_text("❌ Выберите хотя бы одну дату!")
                return SELECTING_DATES
    
    elif query.data == "add_admin":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ У вас нет прав для этой операции.")
            return
        await query.edit_message_text(
            "➕ Для добавления администратора:\n"
            "1. Попросите пользователя отправить команду /id в боте\n"
            "2. Отправьте команду /add_admin <id_пользователя>\n\n"
            "Например: /add_admin 123456789"
        )
    
    elif query.data == "remove_admin":
        if not await is_admin(user_id):
            await query.edit_message_text("❌ У вас нет прав для этой операции.")
            return
        await show_remove_admin_list(query, user_id)
    
    elif query.data.startswith("remove_admin_"):
        if not await is_admin(user_id):
            return
        admin_id_to_remove = int(query.data.replace("remove_admin_", ""))
        if admin_id_to_remove == user_id:
            await query.edit_message_text("❌ Нельзя удалить самого себя!")
            return
        if admin_id_to_remove in ADMINS:
            ADMINS.remove(admin_id_to_remove)
            save_data()
            await query.edit_message_text(f"✅ Администратор {admin_id_to_remove} удален")
            await asyncio.sleep(1)
            await show_admin_management(query)
    
    elif query.data == "list_admins":
        if not await is_admin(user_id):
            return
        await show_admins_list(query)

async def show_remove_admin_list(query, admin_id: int):
    """Показать список админов для удаления"""
    text = "👥 Выберите администратора для удаления:\n\n"
    keyboard = []
    
    for aid in ADMINS:
        if aid != admin_id:  # Не показываем самого себя
            text += f"• ID: {aid}\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Удалить {aid}",
                callback_data=f"remove_admin_{aid}"
            )])
    
    if not keyboard:
        text = "👥 Нет других администраторов для удаления."
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="manage_admins")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def show_admins_list(query):
    """Показать список всех администраторов"""
    text = "👥 Список администраторов:\n\n"
    for aid in ADMINS:
        text += f"• {aid}\n"
    
    text += f"\nВсего: {len(ADMINS)}"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="manage_admins")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def show_admin_management(query):
    """Показать управление администраторами"""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить администратора", callback_data="add_admin")],
        [InlineKeyboardButton("➖ Удалить администратора", callback_data="remove_admin")],
        [InlineKeyboardButton("📋 Список администраторов", callback_data="list_admins")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "👥 Управление администраторами\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def show_suggestions(query, admin_id: int, page: int = 1):
    """Показать предложения от пользователей"""
    if not suggestions:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📨 Нет новых предложений от пользователей.",
            reply_markup=reply_markup
        )
        return
    
    # Сортируем предложения по дате (новые сверху)
    sorted_suggestions = sorted(
        suggestions.items(),
        key=lambda x: x[1].get('created_at', ''),
        reverse=True
    )
    
    suggestions_per_page = 3
    total_pages = math.ceil(len(sorted_suggestions) / suggestions_per_page)
    page = max(1, min(page, total_pages))
    
    start_idx = (page - 1) * suggestions_per_page
    end_idx = min(start_idx + suggestions_per_page, len(sorted_suggestions))
    
    text = f"📨 Предложения от пользователей (страница {page}/{total_pages}):\n\n"
    
    keyboard = []
    
    for i in range(start_idx, end_idx):
        sugg_id, sugg = sorted_suggestions[i]
        user_info = sugg.get('user_info', 'Неизвестно')
        created_at = sugg.get('created_at', '')
        dates = ', '.join(sugg.get('selected_dates', []))
        times = ', '.join(sugg.get('selected_times', []))
        
        media_info = "📸 Медиа-группа" if sugg.get('is_media_group') else "📝 Одиночное сообщение"
        
        text += f"📝 От: {user_info}\n"
        text += f"🆔 ID: {sugg_id[:8]}...\n"
        text += f"📅 Даты: {dates}\n"
        text += f"⏰ Время: {times}\n"
        text += f"📊 Постов в день: {sugg.get('post_count')}\n"
        text += f"📌 {media_info}\n"
        text += f"📅 Создано: {created_at}\n\n"
        
        keyboard.append([
            InlineKeyboardButton(f"✅ Одобрить {i+1}", callback_data=f"approve_{sugg_id}"),
            InlineKeyboardButton(f"❌ Отклонить {i+1}", callback_data=f"reject_{sugg_id}")
        ])
    
    # Навигация
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"view_suggestions_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"view_suggestions_{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def approve_suggestion(query, admin_id: int, suggestion_id: str):
    """Одобрить предложение и запланировать пост"""
    if suggestion_id not in suggestions:
        await query.edit_message_text("❌ Предложение не найдено.")
        return
    
    sugg = suggestions[suggestion_id]
    user_id = sugg.get('user_id')
    
    # Создаем запланированные посты
    moscow_tz = pytz.timezone('Europe/Moscow')
    scheduled_count = 0
    
    for date_str in sugg['selected_dates']:
        day, month = map(int, date_str.split('.'))
        year = datetime.now().year
        
        if month < datetime.now().month:
            year += 1
        
        for time_str in sugg['selected_times']:
            hour = int(time_str.split(':')[0])
            
            scheduled_datetime = datetime(year, month, day, hour, 0, 0)
            scheduled_datetime = moscow_tz.localize(scheduled_datetime)
            
            if scheduled_datetime < datetime.now(moscow_tz):
                continue
            
            post_id = str(uuid.uuid4())
            
            post_data = {
                'id': post_id,
                'user_id': admin_id,
                'original_suggester': user_id,
                'forwarded_messages_info': sugg.get('forwarded_messages_info', []),
                'is_media_group': sugg.get('is_media_group', False),
                'date': scheduled_datetime.date().isoformat(),
                'time': time_str,
                'datetime': scheduled_datetime.isoformat(),
                'chat_id': GROUP_ID,
                'source': sugg.get('source', 'Неизвестно'),
                'created_at': datetime.now().isoformat()
            }
            
            scheduled_messages[post_id] = post_data
            
            trigger = DateTrigger(
                run_date=scheduled_datetime
            )
            
            scheduler.add_job(
                post_scheduler.send_scheduled_message,
                trigger=trigger,
                args=[GROUP_ID, post_data],
                id=f"post_{post_id}",
                replace_existing=True
            )
            
            scheduled_count += 1
    
    # Уведомляем пользователя
    try:
        media_text = " (медиа-группа)" if sugg.get('is_media_group') else ""
        await query.get_bot().send_message(
            chat_id=user_id,
            text=f"✅ Ваше предложение поста{media_text} одобрено администратором!\n"
                 f"📅 Запланировано публикаций: {scheduled_count}"
        )
    except:
        pass
    
    # Удаляем предложение
    del suggestions[suggestion_id]
    save_data()
    
    await query.edit_message_text(
        f"✅ Предложение одобрено!\n"
        f"📊 Запланировано публикаций: {scheduled_count}"
    )
    
    # Возвращаемся к списку предложений
    await asyncio.sleep(2)
    await show_suggestions(query, admin_id, 1)

async def reject_suggestion(query, admin_id: int, suggestion_id: str):
    """Отклонить предложение"""
    if suggestion_id not in suggestions:
        await query.edit_message_text("❌ Предложение не найдено.")
        return
    
    sugg = suggestions[suggestion_id]
    user_id = sugg.get('user_id')
    
    # Уведомляем пользователя
    try:
        await query.get_bot().send_message(
            chat_id=user_id,
            text=f"❌ Ваше предложение поста было отклонено администратором."
        )
    except:
        pass
    
    # Удаляем предложение
    del suggestions[suggestion_id]
    save_data()
    
    await query.edit_message_text("✅ Предложение отклонено")
    
    # Возвращаемся к списку предложений
    await asyncio.sleep(2)
    await show_suggestions(query, admin_id, 1)

async def show_user_posts(query, user_id: int, page: int = 1):
    """Показать запланированные посты (только для админов)"""
    if not await is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав для просмотра этой информации.")
        return
    
    # Собираем все посты
    all_posts = list(scheduled_messages.items())
    
    if not all_posts:
        text = "Нет запланированных постов."
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)
        return
    
    posts_per_page = 5
    total_pages = math.ceil(len(all_posts) / posts_per_page)
    page = max(1, min(page, total_pages))
    
    start_idx = (page - 1) * posts_per_page
    end_idx = min(start_idx + posts_per_page, len(all_posts))
    
    text = f"📋 Все запланированные посты (страница {page}/{total_pages}):\n\n"
    
    keyboard = []
    
    for i in range(start_idx, end_idx):
        post_id, post = all_posts[i]
        source = post.get('source', 'Неизвестно')
        post_date = post.get('date')
        if isinstance(post_date, str):
            try:
                post_date_obj = datetime.fromisoformat(post_date)
                post_date = post_date_obj.strftime('%d.%m.%Y')
            except:
                post_date = str(post_date)
        
        suggester = post.get('original_suggester', 'Админ')
        if suggester != 'Админ':
            suggester = f"Предложил: {suggester}"
        
        media_info = "📸 Медиа-группа" if post.get('is_media_group') else "📝 Текст"
        
        text += f"{i+1}. 📅 {post_date} ⏰ {post.get('time', '')}\n"
        text += f"   📌 {media_info}\n"
        text += f"   📌 {source[:30]}\n"
        text += f"   👤 {suggester}\n"
        text += f"   🆔 {post_id[:6]}...\n\n"
        
        keyboard.append([InlineKeyboardButton(
            f"❌ Удалить пост {i+1}", 
            callback_data=f"delete_{post_id}"
        )])
    
    # Навигация
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"prev_page_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"next_page_{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def show_help(query):
    """Показать справку"""
    user_id = query.from_user.id
    
    if await is_admin(user_id):
        text = (
            "❓ Справка для администраторов:\n\n"
            "📅 **Запланировать пост** - создать новую публикацию\n"
            "📋 **Все запланированные посты** - просмотр всех постов\n"
            "👥 **Управление администраторами** - добавить/удалить админов\n"
            "📨 **Предложения от пользователей** - просмотр и одобрение предложений\n\n"
            "Команды:\n"
            "/start - Главное меню\n"
            "/cancel - Отмена текущего действия\n"
            "/add_admin <id> - добавить администратора\n"
            "/remove_admin <id> - удалить администратора\n"
            "/list_admins - список администраторов\n"
            "/id - узнать свой ID"
        )
    else:
        text = (
            "❓ Справка для пользователей:\n\n"
            "📝 **Предложить пост** - отправить пост на рассмотрение администраторам\n\n"
            "После отправки поста вы сможете выбрать даты и время публикации.\n"
            "Администраторы рассмотрят ваше предложение и, если одобрят, запланируют пост.\n\n"
            "Команды:\n"
            "/start - Главное меню\n"
            "/cancel - Отмена текущего действия\n"
            "/id - узнать свой ID"
        )
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_main_menu(query):
    """Показать главное меню"""
    user_id = query.from_user.id
    
    if await is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton("📅 Запланировать пост", callback_data="schedule_post")],
            [InlineKeyboardButton("📋 Все запланированные посты", callback_data="my_posts_1")],
            [InlineKeyboardButton("👥 Управление администраторами", callback_data="manage_admins")],
            [InlineKeyboardButton("📨 Предложения от пользователей", callback_data="view_suggestions_1")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("📝 Предложить пост", callback_data="suggest_post")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "👋 Главное меню. Выберите действие:",
        reply_markup=reply_markup
    )

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик пересланных сообщений"""
    # Игнорируем сообщения из группы
    if update.message.chat.type != 'private':
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    
    if not update.message.forward_date:
        await update.message.reply_text(
            "❌ Пожалуйста, перешлите сообщение из канала или чата.\n"
            "Для отмены используйте /cancel"
        )
        return ConversationHandler.END
    
    # Проверяем, является ли сообщение частью медиа-группы
    media_group_id = update.message.media_group_id
    
    if media_group_id:
        # Это сообщение из медиа-группы
        if media_group_id not in media_groups:
            # Создаем новую группу
            media_groups[media_group_id] = {
                'user_id': user_id,
                'messages': [],
                'last_update': datetime.now()
            }
        
        # Добавляем сообщение в группу
        media_groups[media_group_id]['messages'].append(update.message)
        media_groups[media_group_id]['last_update'] = datetime.now()
        
        # Ждем немного, чтобы собрать все сообщения группы
        await asyncio.sleep(2)
        
        # Проверяем, есть ли группа и не было ли новых сообщений
        if media_group_id in media_groups:
            # Собираем все сообщения группы
            group_messages = media_groups[media_group_id]['messages']
            
            # Получаем информацию об отправителе из первого сообщения
            first_msg = group_messages[0]
            forward_from = first_msg.forward_from
            forward_from_chat = first_msg.forward_from_chat
            
            if forward_from_chat:
                source = forward_from_chat.title
                source_type = "channel"
            elif forward_from:
                source = forward_from.full_name
                source_type = "user"
            else:
                source = "Неизвестный источник"
                source_type = "unknown"
            
            # Получаем информацию о пользователе
            user = update.effective_user
            user_info = f"@{user.username}" if user.username else f"{user.first_name} {user.last_name or ''}".strip()
            
            # Сохраняем информацию о сообщениях группы
            forwarded_messages_info = []
            for msg in group_messages:
                forwarded_messages_info.append({
                    'message_id': msg.message_id,
                    'chat_id': msg.chat.id,
                    'chat_title': msg.chat.title if hasattr(msg.chat, 'title') else None,
                    'date': msg.date.isoformat() if msg.date else None
                })
            
            user_sessions[user_id] = {
                'forwarded_messages_info': forwarded_messages_info,
                'is_media_group': True,
                'message_text': f"Медиа-группа из {len(group_messages)} сообщений",
                'source': source,
                'source_type': source_type,
                'selected_dates': [],
                'current_month': datetime.now().month,
                'current_year': datetime.now().year,
                'user_info': user_info,
                'user_id': user_id,
                'is_suggestion': not await is_admin(user_id)
            }
            
            # Очищаем временное хранилище
            del media_groups[media_group_id]
            
            # Показываем выбор дат
            await show_date_selection(update.message, user_id)
            return SELECTING_DATES
        
    else:
        # Одиночное сообщение
        forward_from = update.message.forward_from
        forward_from_chat = update.message.forward_from_chat
        
        if forward_from_chat:
            source = forward_from_chat.title
            source_type = "channel"
        elif forward_from:
            source = forward_from.full_name
            source_type = "user"
        else:
            source = "Неизвестный источник"
            source_type = "unknown"
        
        message_text = update.message.text or update.message.caption or "Медиа-сообщение"
        
        # Получаем информацию о пользователе
        user = update.effective_user
        user_info = f"@{user.username}" if user.username else f"{user.first_name} {user.last_name or ''}".strip()
        
        # Сохраняем информацию о сообщении
        forwarded_messages_info = [{
            'message_id': update.message.message_id,
            'chat_id': update.message.chat.id,
            'chat_title': update.message.chat.title if hasattr(update.message.chat, 'title') else None,
            'date': update.message.date.isoformat() if update.message.date else None
        }]
        
        user_sessions[user_id] = {
            'forwarded_messages_info': forwarded_messages_info,
            'is_media_group': False,
            'message_text': message_text,
            'source': source,
            'source_type': source_type,
            'has_media': bool(update.message.photo or update.message.video or update.message.document or update.message.audio),
            'selected_dates': [],
            'current_month': datetime.now().month,
            'current_year': datetime.now().year,
            'user_info': user_info,
            'user_id': user_id,
            'is_suggestion': not await is_admin(user_id)
        }
        
        await show_date_selection(update.message, user_id)
        return SELECTING_DATES

async def show_date_selection(message, user_id: int):
    """Показать выбор дат для публикации"""
    session = user_sessions[user_id]
    current_month = session.get('current_month', datetime.now().month)
    current_year = session.get('current_year', datetime.now().year)
    selected_dates = session.get('selected_dates', [])
    
    days_in_month = monthrange(current_year, current_month)[1]
    
    keyboard = []
    row = []
    
    # Навигация по месяцам
    nav_row = []
    
    prev_month = current_month - 1
    prev_year = current_year
    if prev_month < 1:
        prev_month = 12
        prev_year = current_year - 1
    nav_row.append(InlineKeyboardButton("◀️", callback_data=f"prev_month_{prev_month}_{prev_year}"))
    
    month_names = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                   'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']
    nav_row.append(InlineKeyboardButton(f"{month_names[current_month-1]} {current_year}", callback_data="ignore"))
    
    next_month = current_month + 1
    next_year = current_year
    if next_month > 12:
        next_month = 1
        next_year = current_year + 1
    nav_row.append(InlineKeyboardButton("▶️", callback_data=f"next_month_{next_month}_{next_year}"))
    
    keyboard.append(nav_row)
    
    # Дни недели
    week_days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    row = []
    for day in week_days:
        row.append(InlineKeyboardButton(day, callback_data="ignore"))
    keyboard.append(row)
    
    # Календарь
    first_day = datetime(current_year, current_month, 1).weekday()
    first_day = (first_day + 1) % 7
    
    row = []
    for _ in range(first_day):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))
    
    for day in range(1, days_in_month + 1):
        date_str = f"{day:02d}.{current_month:02d}"
        is_selected = date_str in selected_dates
        
        check_date = datetime(current_year, current_month, day).date()
        is_past = check_date < datetime.now().date()
        
        if is_past:
            row.append(InlineKeyboardButton(f"{day}", callback_data="ignore"))
        else:
            if is_selected:
                row.append(InlineKeyboardButton(f"✅ {day}", callback_data=f"select_date_{day}"))
            else:
                row.append(InlineKeyboardButton(f"{day}", callback_data=f"select_date_{day}"))
        
        if len(row) == 7:
            keyboard.append(row)
            row = []
    
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)
    
    # Кнопки управления
    control_row = []
    if selected_dates:
        control_row.append(InlineKeyboardButton("✅ Завершить выбор", callback_data="finish_dates"))
    control_row.append(InlineKeyboardButton("❌ Отмена", callback_data="cancel_scheduling"))
    keyboard.append(control_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    media_info = "📸 Медиа-группа" if session.get('is_media_group') else "📝 Сообщение"
    selected_text = f"\nВыбрано дат: {len(selected_dates)}" if selected_dates else ""
    
    await message.reply_text(
        f"{media_info} получено!\n"
        f"📅 Выберите даты для публикации:{selected_text}\n"
        f"Можно выбрать несколько дат. Нажмите 'Завершить выбор' когда закончите.",
        reply_markup=reply_markup
    )

async def handle_date_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора дат"""
    query = update.callback_query
    await query.answer()
    
    # Игнорируем если это группа
    if query.message.chat.type != 'private':
        return ConversationHandler.END
    
    user_id = query.from_user.id
    
    if query.data == "cancel_scheduling":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    if query.data.startswith("prev_month_"):
        parts = query.data.split('_')
        month = int(parts[2])
        year = int(parts[3])
        if user_id in user_sessions:
            user_sessions[user_id]['current_month'] = month
            user_sessions[user_id]['current_year'] = year
        await show_date_selection(query.message, user_id)
        return SELECTING_DATES
    
    if query.data.startswith("next_month_"):
        parts = query.data.split('_')
        month = int(parts[2])
        year = int(parts[3])
        if user_id in user_sessions:
            user_sessions[user_id]['current_month'] = month
            user_sessions[user_id]['current_year'] = year
        await show_date_selection(query.message, user_id)
        return SELECTING_DATES
    
    if query.data.startswith("select_date_"):
        day = int(query.data.split('_')[2])
        if user_id in user_sessions:
            current_month = user_sessions[user_id]['current_month']
            current_year = user_sessions[user_id]['current_year']
            date_str = f"{day:02d}.{current_month:02d}"
            
            if date_str in user_sessions[user_id]['selected_dates']:
                user_sessions[user_id]['selected_dates'].remove(date_str)
            else:
                user_sessions[user_id]['selected_dates'].append(date_str)
            
            await show_date_selection(query.message, user_id)
            return SELECTING_DATES
    
    if query.data == "finish_dates":
        if user_id in user_sessions and user_sessions[user_id].get('selected_dates'):
            await show_count_selection(query, user_id)
            return SELECTING_COUNT
        else:
            await query.edit_message_text("❌ Выберите хотя бы одну дату!")
            return SELECTING_DATES
    
    return SELECTING_DATES

async def show_count_selection(query, user_id: int):
    """Показать выбор количества публикаций"""
    keyboard = []
    row = []
    for i in range(1, 6):
        row.append(InlineKeyboardButton(str(i), callback_data=f"count_{i}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_scheduling")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if user_id in user_sessions:
        selected_dates = user_sessions[user_id]['selected_dates']
        dates_text = ', '.join(sorted(selected_dates))
        
        await query.edit_message_text(
            f"📊 Выбрано дат: {len(selected_dates)} ({dates_text})\n\n"
            f"Теперь выберите количество публикаций в день (максимум 5):",
            reply_markup=reply_markup
        )
    else:
        await query.edit_message_text("❌ Сессия не найдена. Начните заново с /start")
        return ConversationHandler.END

async def select_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора количества публикаций"""
    query = update.callback_query
    await query.answer()
    
    if query.message.chat.type != 'private':
        return ConversationHandler.END
    
    if query.data == "cancel_scheduling":
        user_id = query.from_user.id
        if user_id in user_sessions:
            del user_sessions[user_id]
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    count = int(query.data.split('_')[1])
    user_id = query.from_user.id
    
    if user_id in user_sessions:
        user_sessions[user_id]['post_count'] = count
        user_sessions[user_id]['selected_times'] = []
        
        await show_time_selection(query, user_id, 1)
        return SELECTING_TIMES
    else:
        await query.edit_message_text("❌ Сессия не найдена. Начните заново с /start")
        return ConversationHandler.END

async def show_time_selection(query, user_id: int, current_selection: int):
    """Показать выбор времени для публикации"""
    if user_id not in user_sessions:
        await query.edit_message_text("❌ Сессия не найдена. Начните заново с /start")
        return
    
    total_needed = user_sessions[user_id]['post_count']
    selected_times = user_sessions[user_id]['selected_times']
    available_times = [t for t in AVAILABLE_TIMES if t not in selected_times]
    
    if current_selection > total_needed:
        await save_or_suggest(query, user_id)
        return
    
    keyboard = []
    row = []
    
    for time_str in available_times:
        row.append(InlineKeyboardButton(
            time_str, 
            callback_data=f"time_{time_str.replace(':', '_')}"
        ))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    if len(selected_times) > 0:
        keyboard.append([InlineKeyboardButton(
            "✅ Завершить выбор", 
            callback_data="finish_selection"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_scheduling")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    selected_times_str = ', '.join(sorted(selected_times)) if selected_times else 'пока нет'
    
    await query.edit_message_text(
        f"⏰ Выберите время для публикации {current_selection} из {total_needed}\n"
        f"Уже выбрано: {selected_times_str}\n"
        f"Доступное время: с 7:00 до 22:00",
        reply_markup=reply_markup
    )

async def select_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора времени"""
    query = update.callback_query
    await query.answer()
    
    if query.message.chat.type != 'private':
        return ConversationHandler.END
    
    if query.data == "cancel_scheduling":
        user_id = query.from_user.id
        if user_id in user_sessions:
            del user_sessions[user_id]
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    time_str = query.data.replace('time_', '').replace('_', ':')
    
    if user_id in user_sessions:
        selected_times = user_sessions[user_id]['selected_times']
        
        if time_str not in selected_times:
            selected_times.append(time_str)
            
            current_selection = len(selected_times) + 1
            total_needed = user_sessions[user_id]['post_count']
            
            if len(selected_times) < total_needed:
                await show_time_selection(query, user_id, current_selection)
                return SELECTING_TIMES
            else:
                await save_or_suggest(query, user_id)
                return ConversationHandler.END
    
    return SELECTING_TIMES

async def finish_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершить выбор времени"""
    query = update.callback_query
    await query.answer()
    
    if query.message.chat.type != 'private':
        return ConversationHandler.END
    
    if query.data == "cancel_scheduling":
        user_id = query.from_user.id
        if user_id in user_sessions:
            del user_sessions[user_id]
        await query.edit_message_text("❌ Планирование отменено.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    await save_or_suggest(query, user_id)
    return ConversationHandler.END

async def save_or_suggest(query, user_id: int):
    """Сохранить пост или отправить предложение"""
    if user_id not in user_sessions:
        await query.edit_message_text("❌ Произошла ошибка. Попробуйте начать заново с /start")
        return
    
    session = user_sessions[user_id]
    
    if session.get('is_suggestion'):
        # Это предложение от обычного пользователя
        await create_suggestion(query, user_id)
    else:
        # Это прямое планирование от админа
        await save_and_schedule(query, user_id)

async def create_suggestion(query, user_id: int):
    """Создать предложение от пользователя"""
    session = user_sessions[user_id]
    
    suggestion_id = str(uuid.uuid4())
    
    suggestions[suggestion_id] = {
        'id': suggestion_id,
        'user_id': user_id,
        'user_info': session.get('user_info', 'Неизвестно'),
        'message_text': session.get('message_text', 'Медиа-группа'),
        'forwarded_messages_info': session.get('forwarded_messages_info', []),
        'is_media_group': session.get('is_media_group', False),
        'selected_dates': session['selected_dates'],
        'selected_times': session['selected_times'],
        'post_count': session['post_count'],
        'source': session.get('source', 'Неизвестно'),
        'created_at': datetime.now().strftime('%d.%m.%Y %H:%M')
    }
    
    save_data()
    
    # Уведомляем всех админов
    for admin_id in ADMINS:
        try:
            media_info = "📸 Медиа-группа" if session.get('is_media_group') else "📝 Сообщение"
            await query.get_bot().send_message(
                chat_id=admin_id,
                text=f"📨 Новое предложение ({media_info}) от пользователя {session.get('user_info')}!\n"
                     f"📅 Дат: {len(session['selected_dates'])}, ⏰ Время: {len(session['selected_times'])} вариантов\n"
                     f"Используйте /start для просмотра предложений."
            )
        except:
            pass
    
    dates_text = '\n'.join([f"• {d}" for d in sorted(session['selected_dates'])])
    times_text = '\n'.join([f"• {t}" for t in sorted(session['selected_times'])])
    media_info = "📸 Медиа-группа" if session.get('is_media_group') else "📝 Сообщение"
    
    response_text = (
        f"✅ Ваше предложение ({media_info}) отправлено администраторам!\n\n"
        f"📌 Источник: {session.get('source', 'Неизвестно')}\n"
        f"📅 Выбранные даты:\n{dates_text}\n"
        f"⏰ Выбранное время:\n{times_text}\n"
        f"📊 Постов в день: {session['post_count']}\n\n"
        f"⏳ Ожидайте решения администраторов. Вы получите уведомление, когда ваш пост одобрят или отклонят."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(response_text, reply_markup=reply_markup)
    
    # Очищаем сессию
    del user_sessions[user_id]

async def save_and_schedule(query, user_id: int):
    """Сохранить и запланировать публикации (для админов)"""
    session = user_sessions[user_id]
    selected_dates = session['selected_dates']
    selected_times = session['selected_times']
    source = session.get('source', 'Неизвестно')
    forwarded_messages_info = session.get('forwarded_messages_info', [])
    is_media_group = session.get('is_media_group', False)
    
    if not selected_dates or not selected_times:
        await query.edit_message_text("❌ Даты или время не выбраны. Попробуйте снова.")
        return
    
    moscow_tz = pytz.timezone('Europe/Moscow')
    scheduled_count = 0
    
    for date_str in selected_dates:
        day, month = map(int, date_str.split('.'))
        year = datetime.now().year
        
        if month < datetime.now().month:
            year += 1
        
        for time_str in selected_times:
            hour = int(time_str.split(':')[0])
            
            scheduled_datetime = datetime(year, month, day, hour, 0, 0)
            scheduled_datetime = moscow_tz.localize(scheduled_datetime)
            
            if scheduled_datetime < datetime.now(moscow_tz):
                continue
            
            post_id = str(uuid.uuid4())
            
            post_data = {
                'id': post_id,
                'user_id': user_id,
                'forwarded_messages_info': forwarded_messages_info,
                'is_media_group': is_media_group,
                'date': scheduled_datetime.date().isoformat(),
                'time': time_str,
                'datetime': scheduled_datetime.isoformat(),
                'chat_id': GROUP_ID,
                'source': source,
                'created_at': datetime.now().isoformat()
            }
            
            scheduled_messages[post_id] = post_data
            
            trigger = DateTrigger(
                run_date=scheduled_datetime
            )
            
            scheduler.add_job(
                post_scheduler.send_scheduled_message,
                trigger=trigger,
                args=[GROUP_ID, post_data],
                id=f"post_{post_id}",
                replace_existing=True
            )
            
            scheduled_count += 1
    
    save_data()
    
    if scheduled_count == 0:
        await query.edit_message_text("❌ Все выбранные даты уже прошли. Выберите будущие даты.")
        return
    
    dates_text = '\n'.join([f"• {d}" for d in sorted(selected_dates)])
    times_text = '\n'.join([f"• {t}" for t in sorted(selected_times)])
    media_info = "📸 Медиа-группа" if is_media_group else "📝 Сообщение"
    
    response_text = (
        f"✅ Пост успешно запланирован!\n\n"
        f"📝 Тип: {media_info}\n"
        f"📌 Источник: {source[:50]}\n"
        f"📅 Даты публикации:\n{dates_text}\n"
        f"⏰ Время публикаций:\n{times_text}\n"
        f"📊 Всего публикаций: {scheduled_count}\n\n"
        f"🔁 Все публикации будут сделаны как репосты с сохранением авторства."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(response_text, reply_markup=reply_markup)
    
    # Очищаем сессию
    del user_sessions[user_id]

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена действия"""
    # Игнорируем сообщения из группы
    if update.message.chat.type != 'private':
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
    
    await update.message.reply_text(
        "❌ Действие отменено. Используйте /start для начала работы."
    )
    return ConversationHandler.END

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных сообщений"""
    # Игнорируем сообщения из группы
    if update.message.chat.type != 'private':
        return
    
    await update.message.reply_text(
        "Я ожидаю репост сообщения для планирования.\n"
        "Используйте /start для начала работы."
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Произошла ошибка: {context.error}")
    
    if update and update.effective_chat and update.effective_chat.type == 'private':
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Произошла техническая ошибка. Пожалуйста, попробуйте позже или используйте /start"
            )
        except:
            pass

# Команды для управления администраторами
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить администратора (только в личных сообщениях)"""
    if update.message.chat.type != 'private':
        return
    
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Использование: /add_admin <id_пользователя>\n"
            "Чтобы узнать ID пользователя, попросите его отправить /id боту"
        )
        return
    
    try:
        new_admin_id = int(context.args[0])
        ADMINS.add(new_admin_id)
        save_data()
        await update.message.reply_text(f"✅ Пользователь {new_admin_id} добавлен в администраторы")
        
        # Уведомляем нового администратора
        try:
            await context.bot.send_message(
                chat_id=new_admin_id,
                text="🎉 Вас назначили администратором бота для планирования публикаций!\n"
                     "Отправьте /start чтобы начать работу."
            )
        except:
            pass
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить администратора (только в личных сообщениях)"""
    if update.message.chat.type != 'private':
        return
    
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Использование: /remove_admin <id_пользователя>"
        )
        return
    
    try:
        remove_id = int(context.args[0])
        if remove_id == user_id:
            await update.message.reply_text("❌ Нельзя удалить самого себя!")
            return
        if remove_id in ADMINS:
            ADMINS.remove(remove_id)
            save_data()
            await update.message.reply_text(f"✅ Пользователь {remove_id} удален из администраторов")
        else:
            await update.message.reply_text("❌ Пользователь не является администратором")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID")

async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список администраторов (только в личных сообщениях)"""
    if update.message.chat.type != 'private':
        return
    
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    if ADMINS:
        text = "👥 Список администраторов:\n\n"
        for admin_id in ADMINS:
            text += f"• {admin_id}\n"
        text += f"\nВсего: {len(ADMINS)}"
    else:
        text = "❌ Нет администраторов"
    
    await update.message.reply_text(text)

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать свой ID (для добавления в админы)"""
    if update.message.chat.type != 'private':
        return
    
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"🆔 Ваш ID: {user_id}\n\n"
        f"Передайте этот ID администратору, чтобы он добавил вас."
    )

async def restore_scheduled_jobs(app: Application):
    """Восстановление запланированных заданий после перезапуска"""
    logger.info("Восстановление запланированных заданий...")
    
    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(moscow_tz)
    
    restored_count = 0
    for post_id, post_data in scheduled_messages.items():
        try:
            # Получаем datetime из сохраненных данных
            if 'datetime' in post_data:
                if isinstance(post_data['datetime'], str):
                    scheduled_datetime = datetime.fromisoformat(post_data['datetime'])
                else:
                    scheduled_datetime = post_data['datetime']
                
                # Проверяем, что время еще не прошло
                if scheduled_datetime > now:
                    # Добавляем bot в post_data
                    post_data['bot'] = app.bot
                    
                    trigger = DateTrigger(
                        run_date=scheduled_datetime
                    )
                    
                    scheduler.add_job(
                        post_scheduler.send_scheduled_message,
                        trigger=trigger,
                        args=[GROUP_ID, post_data],
                        id=f"post_{post_id}",
                        replace_existing=True
                    )
                    restored_count += 1
                    logger.info(f"Восстановлен пост {post_id} на {scheduled_datetime}")
        except Exception as e:
            logger.error(f"Ошибка восстановления поста {post_id}: {e}")
    
    logger.info(f"Восстановлено {restored_count} запланированных постов")

async def cleanup_media_groups():
    """Очистка старых медиа-групп"""
    while True:
        await asyncio.sleep(600)  # 10 минут
        current_time = datetime.now()
        to_delete = []
        for group_id, group_data in media_groups.items():
            if (current_time - group_data['last_update']).total_seconds() > 300:  # 5 минут
                to_delete.append(group_id)
        for group_id in to_delete:
            del media_groups[group_id]
            logger.info(f"Очищена старая медиа-группа {group_id}")

def main():
    """Основная функция запуска бота"""
    # Загружаем данные
    load_data()
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    
    scheduler.start()
    logger.info("Планировщик запущен")
    
    # Добавляем команды для управления администраторами
    application.add_handler(CommandHandler("add_admin", add_admin_command))
    application.add_handler(CommandHandler("remove_admin", remove_admin_command))
    application.add_handler(CommandHandler("list_admins", list_admins_command))
    application.add_handler(CommandHandler("id", id_command))
    
    # Обработчик для игнорируемых кнопок
    application.add_handler(CallbackQueryHandler(ignore_callback, pattern="^ignore$"))
    
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.FORWARDED, handle_forwarded_message),
            CallbackQueryHandler(button_callback, pattern="^(schedule_post|suggest_post)$"),
        ],
        states={
            SELECTING_DATES: [
                CallbackQueryHandler(handle_date_selection, pattern="^(prev_month_|next_month_|select_date_|finish_dates|cancel_scheduling)"),
            ],
            SELECTING_COUNT: [
                CallbackQueryHandler(select_count, pattern="^(count_[1-5]|cancel_scheduling)$"),
            ],
            SELECTING_TIMES: [
                CallbackQueryHandler(select_time, pattern="^(time_|cancel_scheduling)"),
                CallbackQueryHandler(finish_selection, pattern="^finish_selection$"),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            MessageHandler(filters.FORWARDED, handle_forwarded_message),
        ],
        per_message=False,
        name="post_scheduler_conversation"
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded_message))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_unknown))
    application.add_error_handler(error_handler)
    
    logger.info("Бот запущен...")
    print("Бот запущен... Ожидание сообщений...")
    print(f"Первый администратор (ID: {INITIAL_ADMIN_ID}) уже добавлен!")
    print("✅ Поддерживаются медиа-группы (несколько фото/видео)")
    print("✅ Посты будут публиковаться как РЕПОСТЫ с сохранением авторства!")
    print("Для остановки нажмите Ctrl+C")
    
    # Восстанавливаем запланированные задания
    asyncio.get_event_loop().run_until_complete(restore_scheduled_jobs(application))
    
    # Запускаем очистку медиа-групп
    asyncio.get_event_loop().create_task(cleanup_media_groups())
    
    try:
        # Запускаем бота
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            poll_interval=1.0,
            timeout=30
        )
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        print(f"Критическая ошибка: {e}")
    finally:
        # Останавливаем планировщик при завершении
        scheduler.shutdown()
        save_data()

if __name__ == '__main__':
    main()