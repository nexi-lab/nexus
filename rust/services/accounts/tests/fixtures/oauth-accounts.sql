CREATE TABLE user_oauth_accounts (
	oauth_account_id VARCHAR(36) NOT NULL,
	user_id VARCHAR(255) NOT NULL,
	provider VARCHAR(50) NOT NULL,
	provider_user_id VARCHAR(255) NOT NULL,
	provider_email VARCHAR(255),
	encrypted_id_token TEXT,
	token_expires_at DATETIME,
	provider_profile TEXT,
	created_at DATETIME NOT NULL,
	last_used_at DATETIME,
	PRIMARY KEY (oauth_account_id),
	CONSTRAINT uq_provider_user UNIQUE (provider, provider_user_id),
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX idx_user_oauth_provider ON user_oauth_accounts (provider);
CREATE INDEX idx_user_oauth_provider_user ON user_oauth_accounts (provider, provider_user_id);
CREATE INDEX idx_user_oauth_user ON user_oauth_accounts (user_id);
