import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { api, fetchSession, login, logout } from "./client";
import type {
  Catalog,
  Diagnostics,
  Model,
  ParseResult,
  PipelineDocument,
  PipelineVersion,
  PipelineVersionInfo,
  RenderResult,
  SecretName,
} from "./pipelineTypes";
import type {
  BranchHead,
  CancelResult,
  EnvironmentName,
  LogChunk,
  Page,
  Project,
  RunDetail,
  RunSummary,
  Session,
} from "./types";

export const keys = {
  session: ["session"] as const,
  projects: ["projects"] as const,
  project: (slug: string) => ["projects", slug] as const,
  runs: (slug: string, env: EnvironmentName, offset: number) =>
    ["runs", slug, env, offset] as const,
  allRuns: (offset: number) => ["runs", "all", offset] as const,
  run: (id: number) => ["run", id] as const,
  log: (runId: number, stepId: number) => ["log", runId, stepId] as const,
};

export const RUNS_PAGE_SIZE = 20;

export function useSession() {
  return useQuery<Session>({
    queryKey: keys.session,
    queryFn: fetchSession,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useLogin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { username: string; password: string }) =>
      login(input.username, input.password),
    onSuccess: (session) => {
      client.setQueryData(keys.session, session);
    },
  });
}

export function useLogout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSettled: () => {
      client.clear();
    },
  });
}

export function useProjects() {
  return useQuery({
    queryKey: keys.projects,
    queryFn: ({ signal }) => api.get<Project[]>("/api/projects", signal),
  });
}

export function useProject(slug: string) {
  return useQuery({
    queryKey: keys.project(slug),
    queryFn: ({ signal }) => api.get<Project>(`/api/projects/${encodeURIComponent(slug)}`, signal),
  });
}

export function useEnvironmentRuns(slug: string, env: EnvironmentName, offset: number) {
  return useQuery({
    queryKey: keys.runs(slug, env, offset),
    queryFn: ({ signal }) =>
      api.get<Page<RunSummary>>(
        `/api/projects/${encodeURIComponent(slug)}/environments/${env}/runs?limit=${RUNS_PAGE_SIZE}&offset=${offset}`,
        signal,
      ),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });
}

export function useAllRuns(offset: number) {
  return useQuery({
    queryKey: keys.allRuns(offset),
    queryFn: ({ signal }) =>
      api.get<Page<RunSummary>>(`/api/runs?limit=${RUNS_PAGE_SIZE}&offset=${offset}`, signal),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });
}

export function useRun(id: number) {
  return useQuery({
    queryKey: keys.run(id),
    queryFn: ({ signal }) => api.get<RunDetail>(`/api/runs/${id}`, signal),
    enabled: id > 0,
  });
}

export function useBranchHead(slug: string, env: EnvironmentName, branch: string) {
  return useQuery({
    queryKey: ["head", slug, env, branch] as const,
    queryFn: ({ signal }) =>
      api.get<BranchHead>(
        `/api/projects/${encodeURIComponent(slug)}/environments/${env}/head?branch=${encodeURIComponent(branch)}`,
        signal,
      ),
    enabled: branch.trim() !== "",
    retry: false,
  });
}

export function useStartRun(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { branch: string; commit_sha: string }) =>
      api.post<RunSummary>(
        `/api/projects/${encodeURIComponent(slug)}/environments/${env}/runs`,
        input,
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["runs"] });
      await client.invalidateQueries({ queryKey: keys.projects });
    },
  });
}

export function useCancelRun(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<CancelResult>(`/api/runs/${id}/cancel`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: keys.run(id) });
    },
  });
}

export async function fetchLog(runId: number, stepId: number, offset: number): Promise<LogChunk> {
  return api.get<LogChunk>(`/api/runs/${runId}/steps/${stepId}/log?offset=${offset}`);
}

// ---- pipelines ---------------------------------------------------------------------------

export const pipelineKeys = {
  doc: (slug: string, env: EnvironmentName) => ["pipeline", slug, env] as const,
  versions: (slug: string, env: EnvironmentName) => ["pipeline", slug, env, "versions"] as const,
  catalog: ["pipeline-catalog"] as const,
  secrets: (slug: string, env: EnvironmentName) => ["secrets", slug, env] as const,
};

export function pipelinePath(slug: string, env: EnvironmentName): string {
  return `/api/projects/${encodeURIComponent(slug)}/environments/${env}/pipeline`;
}

export function usePipeline(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: pipelineKeys.doc(slug, env),
    queryFn: ({ signal }) => api.get<PipelineDocument>(pipelinePath(slug, env), signal),
    staleTime: 0,
    gcTime: 0,
  });
}

export function usePipelineVersions(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: pipelineKeys.versions(slug, env),
    queryFn: ({ signal }) =>
      api.get<PipelineVersionInfo[]>(`${pipelinePath(slug, env)}/versions`, signal),
  });
}

export function fetchPipelineVersion(
  slug: string,
  env: EnvironmentName,
  version: number,
): Promise<PipelineVersion> {
  return api.get<PipelineVersion>(`${pipelinePath(slug, env)}/versions/${version}`);
}

export function useCatalog() {
  return useQuery({
    queryKey: pipelineKeys.catalog,
    queryFn: ({ signal }) => api.get<Catalog>("/api/pipelines/catalog", signal),
    staleTime: Infinity,
  });
}

export function useSecretNames(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: pipelineKeys.secrets(slug, env),
    queryFn: async ({ signal }) => {
      const secrets = await api.get<SecretName[]>(
        `/api/projects/${encodeURIComponent(slug)}/environments/${env}/secrets`,
        signal,
      );
      return secrets.map((secret) => secret.name);
    },
  });
}

export interface PipelineContext {
  project: string;
  environment: EnvironmentName;
}

export const pipelineApi = {
  parse: (jenkinsfile: string, context: PipelineContext) =>
    api.post<ParseResult>("/api/pipelines/parse", { jenkinsfile, ...context }),
  render: (model: Model, context: PipelineContext) =>
    api.post<RenderResult>("/api/pipelines/render", { model, ...context }),
  validate: (jenkinsfile: string, context: PipelineContext) =>
    api.post<Diagnostics>("/api/pipelines/validate", { jenkinsfile, ...context }),
  save: (slug: string, env: EnvironmentName, jenkinsfile: string, baseVersion: number) =>
    api.put<PipelineDocument>(pipelinePath(slug, env), {
      jenkinsfile,
      base_version: baseVersion,
    }),
};
