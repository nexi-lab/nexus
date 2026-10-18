CREATE TABLE users (
	user_id VARCHAR(255) NOT NULL,
	username VARCHAR(255),
	email VARCHAR(255),
	display_name VARCHAR(255),
	avatar_url TEXT,
	password_hash VARCHAR(512),
	primary_auth_method VARCHAR(50) NOT NULL,
	external_user_id VARCHAR(255),
	external_user_service VARCHAR(100),
	api_key VARCHAR(255),
	zone_id VARCHAR(255),
	is_global_admin INTEGER NOT NULL,
	is_active INTEGER NOT NULL,
	deleted_at DATETIME,
	email_verified INTEGER NOT NULL,
	user_metadata TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	last_login_at DATETIME,
	PRIMARY KEY (user_id)
);
CREATE INDEX idx_users_active ON users (is_active);
CREATE INDEX idx_users_auth_method ON users (primary_auth_method);
CREATE INDEX idx_users_deleted ON users (deleted_at);
CREATE INDEX idx_users_email ON users (email);
CREATE INDEX idx_users_email_active_deleted ON users (email, is_active, deleted_at);
CREATE INDEX idx_users_external ON users (external_user_service, external_user_id);
CREATE INDEX idx_users_username ON users (username);
CREATE INDEX ix_users_api_key ON users (api_key);
CREATE INDEX ix_users_created_at ON users (created_at);
CREATE INDEX ix_users_last_login_at ON users (last_login_at);
CREATE INDEX ix_users_zone_id ON users (zone_id);
CREATE UNIQUE INDEX uq_users_email_verified_active ON users (email) WHERE is_active = 1 AND email_verified = 1 AND deleted_at IS NULL;
