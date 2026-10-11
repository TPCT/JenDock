CREATE TABLE dm_workload_heartbeats(scope_id varchar(64) NOT NULL,instance_id varchar(100) NOT NULL,observed_at timestamptz NOT NULL,last_outcome varchar(16),PRIMARY KEY(scope_id,instance_id));
CREATE TABLE dm_workload_registry(scope_id varchar(64) PRIMARY KEY,registered_at timestamptz NOT NULL);
CREATE TABLE dm_workload_schedules(scope_id varchar(64) NOT NULL,schedule_id varchar(64) NOT NULL,name varchar(100) NOT NULL,expression varchar(100) NOT NULL,timezone varchar(100) NOT NULL,without_overlapping boolean NOT NULL,last_outcome varchar(16),PRIMARY KEY(scope_id,schedule_id));
CREATE TABLE dm_workload_events(id bigserial PRIMARY KEY,scope_id varchar(64) NOT NULL,instance_id varchar(100) NOT NULL,schedule_id varchar(64),job_id varchar(100),outcome varchar(16) NOT NULL,observed_at timestamptz NOT NULL);
CREATE INDEX dm_workload_events_scope ON dm_workload_events(scope_id,id);
CREATE TABLE dm_workload_retries(scope_id varchar(64) NOT NULL,request_id varchar(80) NOT NULL,request_hash varchar(64) NOT NULL,failure_id varchar(36) NOT NULL UNIQUE,result jsonb NOT NULL,PRIMARY KEY(scope_id,request_id));
