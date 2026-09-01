-- 0004 inbound file detail
--
-- The router parses, acknowledges and dispatches, but until now nothing wrote
-- down that a file arrived. That is a hole in the audit trail: BSC Section
-- U1.6 requires us to evidence what was received as well as what was sent,
-- and a report handler cannot record a count against a row that does not
-- exist.

ALTER TABLE inbound_file
    -- Records in the parsed tree, at every level. Distinguishes an empty
    -- report from one that was never processed.
    ADD COLUMN record_count int,
    -- Set when the handler completed. A file can parse and still fail here:
    -- that is our problem, not the sender's, and does not change the ADT we
    -- returned.
    ADD COLUMN handled_at timestamptz,
    ADD COLUMN handler_error text;

-- A filename is unique across central systems within a month (IDD 2.2.5), so
-- receiving the same name twice means either a resend or a collision. Either
-- way we want to know rather than silently storing both.
CREATE UNIQUE INDEX inbound_file_filename ON inbound_file (filename);