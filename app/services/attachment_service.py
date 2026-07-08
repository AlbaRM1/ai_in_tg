"""
Сервис обработки вложений (изображений и документов) для передачи в LLM.

Функциональность:
- Скачивание файлов из Telegram
- Кодирование изображений в base64 data URL (формат OpenAI Vision)
- Извлечение текста из документов (PDF, текстовые файлы)
- Автоматическое определение типа файла и выбор обработки
"""

import base64
import io
import logging
from pathlib import Path

from aiogram import Bot
from pypdf import PdfReader

logger = logging.getLogger(__name__)

# Расширения текстовых файлов для извлечения контента
TEXT_EXTENSIONS = {
    '.txt', '.md', '.py', '.js', '.ts', '.json', '.csv', '.xml', '.html',
    '.css', '.yaml', '.yml', '.toml', '.sh', '.sql', '.java', '.c', '.cpp',
    '.go', '.rs', '.rb', '.php', '.log'
}

# Максимальная длина извлеченного текста (символов)
MAX_TEXT_LENGTH = 100000


async def download_telegram_file(bot: Bot, file_id: str) -> bytes:
    """
    Скачивает файл из Telegram по file_id.
    
    Args:
        bot: Экземпляр aiogram.Bot
        file_id: Идентификатор файла в Telegram
        
    Returns:
        bytes: Содержимое файла
        
    Raises:
        Exception: При ошибке скачивания
    """
    try:
        logger.info(f"Скачивание файла из Telegram: {file_id}")
        file = await bot.get_file(file_id)
        file_bytes_io = await bot.download_file(file.file_path)
        file_bytes = file_bytes_io.read()
        logger.info(f"Файл успешно скачан, размер: {len(file_bytes)} байт")
        return file_bytes
    except Exception as e:
        logger.error(f"Ошибка скачивания файла {file_id}: {e}", exc_info=True)
        raise


def image_bytes_to_data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """
    Кодирует изображение в base64 data URL.
    
    Args:
        image_bytes: Байты изображения
        mime_type: MIME-тип изображения (по умолчанию image/jpeg)
        
    Returns:
        str: Data URL в формате data:{mime_type};base64,{base64_data}
    """
    b64_encoded = base64.b64encode(image_bytes).decode('utf-8')
    data_url = f"data:{mime_type};base64,{b64_encoded}"
    logger.debug(f"Изображение закодировано в data URL, размер base64: {len(b64_encoded)} символов")
    return data_url


def build_image_part(data_url: str) -> dict:
    """
    Создает image part для OpenAI Vision API.
    
    Args:
        data_url: Data URL изображения
        
    Returns:
        dict: {"type": "image_url", "image_url": {"url": data_url}}
    """
    return {
        "type": "image_url",
        "image_url": {
            "url": data_url
        }
    }


def build_text_part(text: str) -> dict:
    """
    Создает text part для OpenAI API.
    
    Args:
        text: Текстовое содержимое
        
    Returns:
        dict: {"type": "text", "text": text}
    """
    return {
        "type": "text",
        "text": text
    }


async def process_photo(bot: Bot, file_id: str, mime_type: str = "image/jpeg") -> dict:
    """
    Обрабатывает фото: скачивает и кодирует в image part.
    
    Args:
        bot: Экземпляр aiogram.Bot
        file_id: Идентификатор файла в Telegram
        mime_type: MIME-тип изображения
        
    Returns:
        dict: Image part для OpenAI Vision API
        
    Raises:
        Exception: При ошибке обработки
    """
    try:
        logger.info(f"Обработка фото: file_id={file_id}, mime_type={mime_type}")
        image_bytes = await download_telegram_file(bot, file_id)
        data_url = image_bytes_to_data_url(image_bytes, mime_type)
        image_part = build_image_part(data_url)
        logger.info("Фото успешно обработано")
        return image_part
    except Exception as e:
        logger.error(f"Ошибка обработки фото {file_id}: {e}", exc_info=True)
        raise


def detect_mime_type(file_name: str | None, mime_type: str | None) -> str:
    """
    Определяет MIME-тип файла по имени или переданному типу.
    
    Args:
        file_name: Имя файла (опционально)
        mime_type: MIME-тип от Telegram (опционально)
        
    Returns:
        str: MIME-тип файла
    """
    # Если Telegram предоставил MIME-тип — используем его
    if mime_type:
        logger.debug(f"Использован MIME-тип от Telegram: {mime_type}")
        return mime_type
    
    # Определяем по расширению файла
    if file_name:
        extension = Path(file_name).suffix.lower()
        mime_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.pdf': 'application/pdf',
        }
        detected = mime_map.get(extension, 'application/octet-stream')
        logger.debug(f"MIME-тип определен по расширению {extension}: {detected}")
        return detected
    
    # Fallback
    logger.debug("MIME-тип не определен, используется application/octet-stream")
    return 'application/octet-stream'


def is_image_mime(mime_type: str) -> bool:
    """
    Проверяет, является ли файл изображением по MIME-типу.
    
    Args:
        mime_type: MIME-тип файла
        
    Returns:
        bool: True если изображение
    """
    is_img = mime_type.startswith("image/")
    logger.debug(f"MIME-тип {mime_type} {'является' if is_img else 'не является'} изображением")
    return is_img


async def extract_text_from_document(file_bytes: bytes, file_name: str, mime_type: str | None) -> str:
    """
    Извлекает текст из документа (PDF или текстовый файл).
    
    Args:
        file_bytes: Байты файла
        file_name: Имя файла
        mime_type: MIME-тип файла
        
    Returns:
        str: Извлеченный текст или сообщение об ошибке
    """
    detected_mime = detect_mime_type(file_name, mime_type)
    extension = Path(file_name).suffix.lower()
    
    logger.info(f"Извлечение текста из документа: {file_name}, тип: {detected_mime}")
    
    # Обработка PDF
    if detected_mime == 'application/pdf' or extension == '.pdf':
        try:
            logger.debug("Обработка PDF файла")
            pdf_reader = PdfReader(io.BytesIO(file_bytes))
            text_parts = []
            
            for page_num, page in enumerate(pdf_reader.pages, start=1):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
                logger.debug(f"Страница {page_num}: извлечено {len(page_text) if page_text else 0} символов")
            
            extracted_text = "\n\n".join(text_parts)
            logger.info(f"Из PDF извлечено {len(extracted_text)} символов ({len(pdf_reader.pages)} страниц)")
            
            # Ограничение размера
            if len(extracted_text) > MAX_TEXT_LENGTH:
                extracted_text = extracted_text[:MAX_TEXT_LENGTH] + "\n\n[... текст обрезан ...]"
                logger.warning(f"Текст обрезан до {MAX_TEXT_LENGTH} символов")
            
            return extracted_text
            
        except Exception as e:
            error_msg = f"[Не удалось извлечь текст из PDF: {str(e)}]"
            logger.error(f"Ошибка извлечения текста из PDF {file_name}: {e}", exc_info=True)
            return error_msg
    
    # Обработка текстовых файлов
    if detected_mime.startswith('text/') or extension in TEXT_EXTENSIONS:
        try:
            logger.debug("Обработка текстового файла")
            text = file_bytes.decode('utf-8', errors='replace')
            logger.info(f"Извлечено {len(text)} символов из текстового файла")
            
            # Ограничение размера
            if len(text) > MAX_TEXT_LENGTH:
                text = text[:MAX_TEXT_LENGTH] + "\n\n[... текст обрезан ...]"
                logger.warning(f"Текст обрезан до {MAX_TEXT_LENGTH} символов")
            
            return text
            
        except Exception as e:
            error_msg = f"[Не удалось декодировать текстовый файл: {str(e)}]"
            logger.error(f"Ошибка декодирования текстового файла {file_name}: {e}", exc_info=True)
            return error_msg
    
    # Неподдерживаемый формат
    error_msg = f"[Файл {file_name} не поддерживается для извлечения текста (тип: {detected_mime})]"
    logger.warning(error_msg)
    return error_msg


async def process_document(bot: Bot, file_id: str, file_name: str, mime_type: str | None) -> dict:
    """
    Обрабатывает документ: автоматически определяет тип и извлекает контент.
    
    - Если изображение → возвращает image part
    - Если документ → извлекает текст и возвращает text part
    
    Args:
        bot: Экземпляр aiogram.Bot
        file_id: Идентификатор файла в Telegram
        file_name: Имя файла
        mime_type: MIME-тип от Telegram (опционально)
        
    Returns:
        dict: Image part или text part в зависимости от типа файла
        
    Raises:
        Exception: При ошибке обработки
    """
    try:
        detected_mime = detect_mime_type(file_name, mime_type)
        logger.info(f"Обработка документа: {file_name}, определен тип: {detected_mime}")
        
        # Если это изображение — обрабатываем как фото
        if is_image_mime(detected_mime):
            logger.info("Документ является изображением, обработка как фото")
            return await process_photo(bot, file_id, detected_mime)
        
        # Иначе скачиваем и извлекаем текст
        logger.info("Документ является файлом, извлечение текста")
        file_bytes = await download_telegram_file(bot, file_id)
        extracted_text = await extract_text_from_document(file_bytes, file_name, mime_type)
        
        # Формируем text part с заголовком файла
        content = f"📎 Содержимое файла '{file_name}':\n\n{extracted_text}"
        text_part = build_text_part(content)
        
        logger.info(f"Документ успешно обработан: {file_name}")
        return text_part
        
    except Exception as e:
        logger.error(f"Ошибка обработки документа {file_name}: {e}", exc_info=True)
        raise
