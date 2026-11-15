-- The exact image a run used: a tag can move, the digest cannot.
alter table runs add column image_digest text;
