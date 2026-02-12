-- Exchange Audit Log: Immutability trigger
-- Related: Issue #1360
--
-- Run this migration against the PostgreSQL metadata database
-- AFTER the SQLAlchemy models have created the exchange_audit_log table.
-- This provides database-level immutability as a defense-in-depth measure
-- (in addition to the ORM event guard in exchange_audit_logger.py).

CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Exchange audit log records are immutable: % not allowed',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

-- Guard against UPDATE
CREATE TRIGGER exchange_audit_log_no_update
    BEFORE UPDATE ON exchange_audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();

-- Guard against DELETE
CREATE TRIGGER exchange_audit_log_no_delete
    BEFORE DELETE ON exchange_audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
