-- ========================================================
-- CloudRestore Database Backup Snapshot
-- Target: Amazon S3 (ap-southeast-2)
-- Integrity: SHA-256 Verified Backup
-- ========================================================

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id UUID PRIMARY KEY,
    action VARCHAR(50) NOT NULL,
    entity_key VARCHAR(255) NOT NULL,
    performed_by VARCHAR(50) NOT NULL,
    s3_version_id VARCHAR(100),
    checksum_sha256 CHAR(64),
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Initial Seed Records
INSERT INTO users (username, email, status) VALUES 
('admin_user', 'admin@cloudrestore.internal', 'ACTIVE'),
('backup_operator', 'operator@cloudrestore.internal', 'ACTIVE'),
('dev_lead', 'developer@cloudrestore.internal', 'ACTIVE');

-- Snapshot Checkpoint Completed: Point-In-Time Backup Ready
