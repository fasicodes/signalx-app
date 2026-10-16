-- Signals FM database (MySQL).
--
-- You normally do NOT need this file: the app creates and upgrades every table by itself when it starts
-- (db.py -> init_db(), plus the init functions of autotrade, papertrade, signal hub, news, track record ...).
-- It only needs a database to exist and the DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME variables.
--
-- To create the empty database by hand:
--   mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS signalx_db CHARACTER SET utf8mb4;
USE signalx_db;

-- The users table as the app creates it (email is the login; Google accounts have no password).
CREATE TABLE IF NOT EXISTS users (
    id INT PRIMARY KEY AUTO_INCREMENT,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NULL,
    auth_provider VARCHAR(20) NOT NULL DEFAULT 'password',
    email_verified TINYINT(1) NOT NULL DEFAULT 0,
    verify_token VARCHAR(64) NULL,          -- SHA-256 hash of the emailed token
    verify_token_expires DATETIME NULL,
    reset_token VARCHAR(64) NULL,           -- SHA-256 hash of the emailed token
    reset_token_expires DATETIME NULL,
    avatar_url VARCHAR(500) NULL,
    session_version INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
