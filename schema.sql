-- SignalX App Database Schema (MySQL)
-- Run this once to create the database + users table:
--   mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS signalx_db;
USE signalx_db;

CREATE TABLE IF NOT EXISTS users (
    id INT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
