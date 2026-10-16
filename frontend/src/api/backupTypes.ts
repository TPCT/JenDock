import type { EnvironmentName } from "./types";

export interface DatabaseBackup {
  id: number;
  project: string;
  environment: EnvironmentName;
  connection: string;
  trigger: "schedule" | "manual";
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  size_bytes: number | null;
  object_key: string | null;
  error: string | null;
}

export interface S3BackupSettings {
  access_key_id: string;
  bucket: string;
  region: string;
  prefix: string;
  hour: number;
  enabled: boolean;
  mysql_image: string;
  mariadb_image: string;
  postgres_image: string;
  secret_configured: boolean;
  iam_policy: string;
}

export type S3BackupSettingsBody = Omit<S3BackupSettings, "secret_configured" | "iam_policy"> & {
  secret_key?: string;
};
