"""
Модуль шифрования API-ключей.
Сейчас это заглушки (pass-through), но интерфейс готов для подключения cryptography.Fernet.

Для активации шифрования:
1. Сгенерировать ключ: Fernet.generate_key()
2. Сохранить в ENCRYPTION_KEY в .env
3. Раскомментировать код с Fernet ниже
"""

from app.config import settings

# from cryptography.fernet import Fernet


def encrypt(value: str) -> str:
    """
    Шифрует строку (например, API-ключ).
    
    Args:
        value: Исходное значение
        
    Returns:
        Зашифрованное значение (сейчас — pass-through)
    """
    # TODO: Подключить реальное шифрование при наличии ключа
    # if settings.ENCRYPTION_KEY:
    #     fernet = Fernet(settings.ENCRYPTION_KEY.encode())
    #     return fernet.encrypt(value.encode()).decode()
    return value


def decrypt(value: str) -> str:
    """
    Расшифровывает строку.
    
    Args:
        value: Зашифрованное значение
        
    Returns:
        Исходное значение (сейчас — pass-through)
    """
    # TODO: Подключить реальную расшифровку при наличии ключа
    # if settings.ENCRYPTION_KEY:
    #     fernet = Fernet(settings.ENCRYPTION_KEY.encode())
    #     return fernet.decrypt(value.encode()).decode()
    return value
