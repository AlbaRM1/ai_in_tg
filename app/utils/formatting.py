"""
Утилиты форматирования текста для Telegram (HTML parse_mode).
Обработка thinking-блоков, code blocks, HTML-escape, конвертация Markdown→HTML.
"""

import html
import logging
import re

logger = logging.getLogger(__name__)


def escape_html(text: str) -> str:
    """
    Экранирует HTML-спецсимволы для безопасного использования в Telegram HTML parse_mode.

    Args:
        text: Исходный текст

    Returns:
        Экранированный текст
    """
    return html.escape(text)


def parse_thinking_blocks(text: str) -> str:
    """
    Парсит thinking/reasoning блоки и оборачивает их в Telegram expandable blockquote.
    
    Ищет блоки вида:
    - <think>...</think>
    - <thinking>...</thinking>
    - Подобные паттерны
    
    Преобразует в: <blockquote expandable>💭 Thought Process\n...\n</blockquote>

    Args:
        text: Исходный текст с возможными thinking-блоками

    Returns:
        Текст с преобразованными блоками
    """
    # Паттерн для различных вариантов thinking-блоков
    patterns = [
        r"<think>(.*?)</think>",
        r"<thinking>(.*?)</thinking>",
        r"<thought>(.*?)</thought>",
    ]

    for pattern in patterns:
        text = re.sub(
            pattern,
            lambda m: f'<blockquote expandable>💭 Thought Process\n{m.group(1).strip()}\n</blockquote>',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    return text


def markdown_to_telegram_html(text: str) -> str:
    """
    Конвертирует Markdown в Telegram HTML.
    
    Поддерживаемые конструкции:
    - Блоки кода ```lang\\ncode\\n``` и ``` code ```
    - Инлайн-код `code`
    - Жирный **text** и __text__
    - Курсив *text* и _text_
    - Зачёркнутый ~~text~~
    - Ссылки [text](url)
    - Заголовки # ... ######
    - Списки - / * / +
    - Горизонтальные линии ---/***
    
    Args:
        text: Markdown текст
        
    Returns:
        Telegram HTML текст
    """
    # Словарь для хранения плейсхолдеров (код-блоки, инлайн-код, ссылки)
    placeholders = {}
    placeholder_counter = [0]  # Используем список для мутабельности в замыкании
    
    def make_placeholder(content: str) -> str:
        """Создаёт уникальный плейсхолдер и сохраняет контент."""
        key = f"\x00PLACEHOLDER_{placeholder_counter[0]}\x00"
        placeholder_counter[0] += 1
        placeholders[key] = content
        return key
    
    # 1. Вырезаем блоки кода (```...```) и заменяем плейсхолдерами
    # Единая регулярка: group(1) — опциональный язык, group(2) — код.
    # Покрывает оба случая: с языком (```python\n...\n```) и без (```\n...\n```).
    def replace_code_block(match):
        lang = match.group(1).strip() if match.group(1) else ""
        code = match.group(2)
        # Экранируем содержимое блока кода
        escaped_code = escape_html(code.strip())
        if lang:
            html_code = f'<pre><code class="language-{lang}">{escaped_code}</code></pre>'
        else:
            html_code = f'<pre><code>{escaped_code}</code></pre>'
        return make_placeholder(html_code)

    # Единый паттерн: опциональный язык (group 1) + код (group 2)
    text = re.sub(r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```", replace_code_block, text, flags=re.DOTALL)
    
    # 2. Вырезаем инлайн-код (`code`) и заменяем плейсхолдерами
    def replace_inline_code(match):
        code = match.group(1)
        escaped_code = escape_html(code)
        return make_placeholder(f"<code>{escaped_code}</code>")
    
    text = re.sub(r"`([^`]+)`", replace_inline_code, text)
    
    # 3. Вырезаем ссылки [text](url) и заменяем плейсхолдерами
    def replace_link(match):
        link_text = match.group(1)
        url = match.group(2)
        return make_placeholder(f'<a href="{escape_html(url)}">{escape_html(link_text)}</a>')
    
    text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', replace_link, text)
    
    # 4. Конвертируем остальную Markdown-разметку в HTML
    # Важно: обрабатываем ** перед *, чтобы не конфликтовали
    
    # Заголовки (# ... ######) в начале строки → жирный текст
    def replace_heading(match):
        heading_text = match.group(2).strip()
        return f"<b>{heading_text}</b>\n"
    
    text = re.sub(r'^(#{1,6})\s+(.+)$', replace_heading, text, flags=re.MULTILINE)
    
    # Жирный: **text** и __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # Курсив: *text* и _text_ (но не внутри слов и не внутри плейсхолдеров)
    text = re.sub(r'(?<!\w)\*([^\*]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<i>\1</i>', text)
    
    # Зачёркнутый: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # Списки: маркеры - / * / + в начале строки → bullet •
    text = re.sub(r'^[\-\*\+]\s+', '• ', text, flags=re.MULTILINE)
    
    # Горизонтальные линии (---, ***, ___ на отдельной строке)
    text = re.sub(r'^[\-\*_]{3,}\s*$', '──────────────────', text, flags=re.MULTILINE)
    
    # 5. Экранируем HTML-спецсимволы в тексте вне тегов и плейсхолдеров
    def escape_non_tag(text: str) -> str:
        """Экранирует текст, сохраняя плейсхолдеры и HTML-теги."""
        parts = []
        last_end = 0
        # Паттерн для поиска плейсхолдеров (PLACEHOLDER и THINKING с null-байтами) и HTML-тегов
        pattern = r'(\x00(?:PLACEHOLDER|THINKING)_\d+\x00|</?[a-z]+(?:\s[^>]*)?>)'
        for match in re.finditer(pattern, text):
            # Экранируем текст до плейсхолдера/тега
            parts.append(escape_html(text[last_end:match.start()]))
            # Добавляем плейсхолдер/тег как есть
            parts.append(match.group(0))
            last_end = match.end()
        # Экранируем хвост
        parts.append(escape_html(text[last_end:]))
        return "".join(parts)
    
    text = escape_non_tag(text)
    
    # 6. Возвращаем плейсхолдеры обратно
    for placeholder, content in placeholders.items():
        text = text.replace(placeholder, content)
    
    return text


def format_for_telegram(text: str, parse_thinking: bool = True) -> str:
    """
    Полное форматирование текста для отправки в Telegram с HTML parse_mode.
    Конвертирует Markdown в Telegram HTML и обрабатывает thinking-блоки.

    Args:
        text: Исходный текст (может содержать Markdown и thinking-блоки)
        parse_thinking: Парсить ли thinking-блоки

    Returns:
        Отформатированный Telegram HTML текст
    """
    try:
        # 1. Парсим thinking-блоки (до конвертации markdown, чтобы не затронуть их содержимое)
        if parse_thinking:
            # Вырезаем thinking-блоки, конвертируем их содержимое отдельно
            thinking_blocks = {}
            thinking_counter = [0]

            def extract_thinking(match):
                content = match.group(1).strip()
                # Конвертируем markdown внутри thinking-блока
                converted_content = markdown_to_telegram_html(content)
                key = f"\x00THINKING_{thinking_counter[0]}\x00"
                thinking_counter[0] += 1
                thinking_blocks[key] = f'<blockquote expandable>💭 Thought Process\n{converted_content}\n</blockquote>'
                return key

            patterns = [
                r"<think>(.*?)</think>",
                r"<thinking>(.*?)</thinking>",
                r"<thought>(.*?)</thought>",
            ]

            for pattern in patterns:
                text = re.sub(pattern, extract_thinking, text, flags=re.DOTALL | re.IGNORECASE)

            # 2. Конвертируем основной текст из Markdown в HTML
            text = markdown_to_telegram_html(text)

            # 3. Возвращаем thinking-блоки
            for key, block in thinking_blocks.items():
                text = text.replace(key, block)
        else:
            # Просто конвертируем markdown
            text = markdown_to_telegram_html(text)

        return text

    except Exception as exc:
        # Безопасный fallback: при любой ошибке форматирования возвращаем
        # экранированный plain-text, чтобы бот гарантированно отправил ответ.
        logger.warning("Ошибка форматирования Markdown→HTML, fallback на plain text: %s", exc)
        return escape_html(text)


def sanitize_for_streaming(text: str) -> str:
    """
    Санитизирует текст для безопасной отправки во время streaming.
    Во время streaming отправляем plain text (без parse_mode),
    чтобы избежать ошибок парсинга неполных тегов.

    Args:
        text: Исходный текст

    Returns:
        Текст для streaming (plain)
    """
    # Просто возвращаем как есть, без HTML-форматирования
    return text


def split_plain_text(text: str, limit: int = 4096) -> list[str]:
    """
    Разбивает plain-текст на части, не превышающие лимит символов Telegram.
    Режет по границам строк и пробелов для читабельности.
    
    Args:
        text: Plain-текст для нарезки
        limit: Максимальная длина одной части (по умолчанию 4096 — лимит Telegram)
        
    Returns:
        Список частей plain-текста, каждая ≤ limit символов
    """
    if not text:
        return []
    
    if len(text) <= limit:
        return [text]
    
    parts = []
    current_part = ""
    
    # Режем по строкам
    lines = text.split('\n')
    
    for line in lines:
        # Если одна строка длиннее лимита — режем по словам/символам
        if len(line) > limit:
            # Сохраняем накопленное
            if current_part:
                parts.append(current_part)
                current_part = ""
            
            # Режем длинную строку по словам
            words = line.split(' ')
            for word in words:
                # Если слово само по себе длиннее лимита — режем посимвольно
                if len(word) > limit:
                    if current_part:
                        parts.append(current_part)
                        current_part = ""
                    
                    # Режем слово на части по limit символов
                    for i in range(0, len(word), limit):
                        parts.append(word[i:i + limit])
                else:
                    # Проверяем, поместится ли слово
                    test_part = current_part + (' ' if current_part else '') + word
                    if len(test_part) <= limit:
                        current_part = test_part
                    else:
                        # Не помещается — сохраняем текущую часть и начинаем новую
                        if current_part:
                            parts.append(current_part)
                        current_part = word
            
            # Добавляем перевод строки к current_part
            if current_part:
                if len(current_part) + 1 <= limit:
                    current_part += '\n'
                else:
                    parts.append(current_part)
                    current_part = '\n'
        else:
            # Обычная строка
            test_part = current_part + ('\n' if current_part else '') + line
            if len(test_part) <= limit:
                current_part = test_part
            else:
                # Не помещается — сохраняем текущую часть
                if current_part:
                    parts.append(current_part)
                current_part = line
    
    # Добавляем последнюю часть
    if current_part:
        parts.append(current_part)
    
    return parts


def split_html_for_telegram(html: str, limit: int = 4096) -> list[str]:
    """
    Разбивает HTML-текст на части, не превышающие лимит символов Telegram.
    Корректно обрабатывает HTML-теги: не разрывает теги, сохраняет вложенность,
    закрывает незакрытые теги в конце части и заново открывает в начале следующей.
    
    Алгоритм:
    1. Парсит HTML на токены (теги, текст, HTML-сущности)
    2. Собирает части, отслеживая открытые теги в стеке
    3. При приближении к лимиту ищет безопасную точку разреза
    4. Закрывает открытые теги в конце части (в обратном порядке)
    5. Открывает их заново в начале следующей части
    
    Поддерживаемые теги: b, i, u, s, code, pre, a, tg-spoiler, blockquote
    
    Args:
        html: HTML-текст после format_for_telegram()
        limit: Максимальная длина одной части (по умолчанию 4096 — лимит Telegram)
        
    Returns:
        Список частей HTML-текста, каждая ≤ limit символов
    """
    if not html or not html.strip():
        return []
    
    # Регулярное выражение для парсинга HTML-токенов
    token_pattern = re.compile(
        r'(<[^>]+>|&[a-zA-Z0-9#]+;|[^<&]+|[<&])',
        re.DOTALL
    )
    
    tokens = token_pattern.findall(html)
    if not tokens:
        return [html] if len(html) <= limit else [html[:limit]]
    
    # Регулярки для парсинга тегов
    opening_tag_re = re.compile(r'^<([a-zA-Z0-9\-]+)(\s[^>]*)?>$')
    closing_tag_re = re.compile(r'^</([a-zA-Z0-9\-]+)>$')
    
    parts = []
    current_tokens = []
    tag_stack = []  # [(tag_name, full_opening_tag), ...]
    
    def finalize_part(tokens_to_finalize: list[str], stack: list[tuple[str, str]]) -> str:
        """Финализирует часть: добавляет закрывающие теги"""
        closing = ''.join(f'</{name}>' for name, _ in reversed(stack))
        return ''.join(tokens_to_finalize) + closing
    
    def start_new_part(stack: list[tuple[str, str]]) -> list[str]:
        """Начинает новую часть: возвращает список с открывающими тегами"""
        return [''.join(tag for _, tag in stack)] if stack else []
    
    def calc_length(tokens_list: list[str]) -> int:
        """Вычисляет длину списка токенов"""
        return sum(len(t) for t in tokens_list)
    
    for token in tokens:
        # Определяем тип токена
        opening_match = opening_tag_re.match(token) if token.startswith('<') else None
        closing_match = closing_tag_re.match(token) if token.startswith('<') and not opening_match else None
        
        # КРИТИЧНО: Проверяем длину ПЕРЕД обновлением стека тегов
        # Вычисляем длину закрывающих тегов для ТЕКУЩЕГО стека (до добавления нового тега)
        closing_tags_len = sum(len(name) + 3 for name, _ in tag_stack)  # </name>
        
        # Если это открывающий тег, резервируем место и для него
        extra_closing_tag_len = 0
        if opening_match and not token.endswith('/>'):
            tag_name = opening_match.group(1).lower()
            extra_closing_tag_len = len(tag_name) + 3  # </tag_name>
        
        # Проверяем, поместится ли токен с учётом ВСЕХ закрывающих тегов
        potential_length = calc_length(current_tokens) + len(token) + closing_tags_len + extra_closing_tag_len
        
        if potential_length > limit:
            # Токен не помещается в текущую часть
            if current_tokens:
                # Завершаем текущую часть
                part = finalize_part(current_tokens, tag_stack)
                parts.append(part)
                current_tokens = start_new_part(tag_stack)
            
            # Проверяем, поместится ли токен в новую часть
            opening_tags_str = ''.join(t for _, t in tag_stack)
            new_potential_length = len(opening_tags_str) + len(token) + closing_tags_len + extra_closing_tag_len
            
            if new_potential_length > limit:
                # Токен слишком длинный даже для новой части
                if not token.startswith('<') and not (token.startswith('&') and token.endswith(';')):
                    # Режем текстовый токен на фрагменты
                    available = limit - len(opening_tags_str) - closing_tags_len - extra_closing_tag_len
                    
                    if available > 100:  # Минимальный размер фрагмента
                        pos = 0
                        while pos < len(token):
                            chunk = token[pos:pos + available]
                            chunk_tokens = start_new_part(tag_stack)
                            chunk_tokens.append(chunk)
                            part = finalize_part(chunk_tokens, tag_stack)
                            parts.append(part)
                            pos += available
                        
                        # Начинаем чистую новую часть
                        current_tokens = start_new_part(tag_stack)
                    else:
                        # Слишком мало места — добавляем как есть
                        current_tokens.append(token)
                else:
                    # HTML-сущность или тег — не режем
                    current_tokens.append(token)
            else:
                # Токен помещается в новую часть
                current_tokens.append(token)
        else:
            # Токен помещается в текущую часть
            current_tokens.append(token)
        
        # Теперь обновляем стек тегов ПОСЛЕ проверки и добавления токена
        if opening_match and not token.endswith('/>'):
            # Открывающий тег (не самозакрывающийся)
            tag_name = opening_match.group(1).lower()
            tag_stack.append((tag_name, token))
        elif closing_match:
            # Закрывающий тег
            tag_name = closing_match.group(1).lower()
            # Удаляем соответствующий открывающий тег из стека
            for idx in range(len(tag_stack) - 1, -1, -1):
                if tag_stack[idx][0] == tag_name:
                    tag_stack.pop(idx)
                    break
    
    # Финализируем последнюю часть
    if current_tokens:
        part = finalize_part(current_tokens, tag_stack)
        parts.append(part)
    
    return parts
