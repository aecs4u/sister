-- Restore the stricter constraints and drop the identities (the added columns and unique rules stay: harmless). Fails if the data now violates a constraint (by design).
ALTER TABLE sister_archive.document_xml_node ALTER COLUMN id DROP IDENTITY IF EXISTS;
ALTER TABLE sister_archive.document_xml_attribute ALTER COLUMN id DROP IDENTITY IF EXISTS;
ALTER TABLE sister.ownership_right ADD CONSTRAINT ck_ownership_right_period
    CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date);
ALTER TABLE sister.document ADD CONSTRAINT ck_document_file_format CHECK (file_format IN ('P7M', 'PDF', 'ZIP', 'DAT'));
