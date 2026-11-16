-- Flows can have several steps; each run belongs to one step.
alter table runs add column step text not null default 'main';
-- Set by a full retry: steps that succeeded before this moment are run again.
alter table jobs add column rerun_from timestamptz;
