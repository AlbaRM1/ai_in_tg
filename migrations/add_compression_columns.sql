-- Migration: Add compression columns to messages table
-- Date: 2026-07-14
-- Description: Adds columns for message compression tracking and summary identification

-- Add compressed column to track if message was compressed
ALTER TABLE messages ADD COLUMN compressed BOOLEAN NOT NULL DEFAULT FALSE;

-- Add is_summary column to identify summary messages
ALTER TABLE messages ADD COLUMN is_summary BOOLEAN NOT NULL DEFAULT FALSE;

-- Create index for efficient queries by session_id and compressed status
CREATE INDEX idx_session_compressed ON messages (session_id, compressed);

-- Add column comments (PostgreSQL only, will be ignored by SQLite)
COMMENT ON COLUMN messages.compressed IS 'Сообщение сжато (заменено резюме), не загружается в контекст';
COMMENT ON COLUMN messages.is_summary IS 'Это резюме-сообщение (результат LLM-сжатия)';
