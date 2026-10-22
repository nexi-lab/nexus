CREATE TABLE zone_runtime_outbox (
	id INTEGER NOT NULL,
	operation_id VARCHAR(64) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	generation INTEGER NOT NULL,
	fence BIGINT NOT NULL,
	attempt_count INTEGER NOT NULL,
	next_retry_at DATETIME,
	created_at DATETIME NOT NULL,
	processed_at DATETIME,
	PRIMARY KEY (id)
);

CREATE INDEX ix_zone_runtime_outbox_pending ON zone_runtime_outbox (processed_at, next_retry_at);

CREATE TABLE zone_grant_projection_outbox (
	id INTEGER NOT NULL,
	grant_id VARCHAR(64) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	generation INTEGER NOT NULL,
	fence BIGINT NOT NULL,
	attempt_count INTEGER NOT NULL,
	next_retry_at DATETIME,
	created_at DATETIME NOT NULL,
	processed_at DATETIME,
	PRIMARY KEY (id)
);

CREATE INDEX ix_zone_grant_projection_outbox_pending ON zone_grant_projection_outbox (processed_at, next_retry_at);
