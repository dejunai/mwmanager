-- Minimal first-pass catalog: exactly two tables in game_catalog.sqlite3.
-- Record bodies and BSA members are stored with source provenance.

CREATE TABLE IF NOT EXISTS esm_record (
  record_id INTEGER PRIMARY KEY,
  source_file TEXT NOT NULL,
  source_sha256 BLOB NOT NULL CHECK (length(source_sha256) = 32),
  load_order INTEGER NOT NULL CHECK (load_order >= 0),
  record_ordinal INTEGER NOT NULL,
  file_offset INTEGER NOT NULL,
  record_tag TEXT NOT NULL CHECK (length(record_tag) = 4),
  record_flags INTEGER NOT NULL,
  editor_id_raw BLOB,
  editor_id TEXT,
  editor_id_norm TEXT,
  body_size INTEGER NOT NULL,
  body_sha256 BLOB NOT NULL CHECK (length(body_sha256) = 32),
  body BLOB NOT NULL,
  UNIQUE (source_file, record_ordinal)
);
CREATE INDEX IF NOT EXISTS ix_esm_record_tag_id ON esm_record(record_tag, editor_id_norm);
CREATE INDEX IF NOT EXISTS ix_esm_record_id ON esm_record(editor_id_norm);

CREATE TABLE IF NOT EXISTS bsa_asset (
  asset_id INTEGER PRIMARY KEY,
  archive_file TEXT NOT NULL,
  archive_sha256 BLOB NOT NULL CHECK (length(archive_sha256) = 32),
  asset_path TEXT NOT NULL,
  asset_path_norm TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  stored_size INTEGER NOT NULL,
  UNIQUE (archive_file, asset_path_norm)
);
CREATE INDEX IF NOT EXISTS ix_bsa_asset_path ON bsa_asset(asset_path_norm);
