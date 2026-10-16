import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { DatabaseBackup, S3BackupSettings, S3BackupSettingsBody } from "./backupTypes";
import type { EnvironmentName } from "./types";

const environmentBackups = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/backups`;

export function useBackups() {
  return useQuery({
    queryKey: ["backups"],
    queryFn: ({ signal }) => api.get<DatabaseBackup[]>("/api/backups", signal),
    refetchInterval: (query) =>
      query.state.data?.some((backup) => backup.status === "running") ? 5_000 : false,
  });
}

export function useEnvironmentBackups(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: ["backups", slug, env],
    queryFn: ({ signal }) => api.get<DatabaseBackup[]>(environmentBackups(slug, env), signal),
    refetchInterval: (query) =>
      query.state.data?.some((backup) => backup.status === "running") ? 5_000 : false,
  });
}

export function useRunBackup(slug: string, env: EnvironmentName, connection: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<DatabaseBackup>(
        `${environmentBackups(slug, env).replace(/\/backups$/, "")}/databases/${encodeURIComponent(connection)}/backups`,
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["backups"] });
    },
  });
}

export function useSetBackupEnvironment(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (include_in_backups: boolean) =>
      api.patch(`/api/projects/${encodeURIComponent(slug)}/environments/${env}/backups`, {
        include_in_backups,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

export function useS3BackupSettings() {
  return useQuery({
    queryKey: ["integrations", "s3-backups"],
    queryFn: ({ signal }) => api.get<S3BackupSettings>("/api/integrations/s3-backups", signal),
  });
}

export function useSaveS3BackupSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: S3BackupSettingsBody) =>
      api.put<S3BackupSettings>("/api/integrations/s3-backups", body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["integrations", "s3-backups"] });
    },
  });
}

export function useTestS3BackupSettings() {
  return useMutation({
    mutationFn: () =>
      api.post<{ ok: boolean; message: string }>("/api/integrations/s3-backups/test"),
  });
}
