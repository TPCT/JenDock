import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  Certificate,
  Check,
  CreateDomainBody,
  CreateProjectBody,
  CreateTokenBody,
  CreatedToken,
  Domain,
  DomainDetail,
  GitTransport,
  PipelineTemplate,
  Plan,
  PlanOperation,
  SecretInfo,
  TokenInfo,
} from "./adminTypes";
import { keys, pipelineKeys } from "./hooks";
import type { PipelineDocument } from "./pipelineTypes";
import type { EnvironmentName, EnvironmentSummary, Project, RunSummary } from "./types";

const projectPath = (slug: string) => `/api/projects/${encodeURIComponent(slug)}`;
const envPath = (slug: string, env: EnvironmentName) => `${projectPath(slug)}/environments/${env}`;

// ---- projects ----------------------------------------------------------------------------

export function useTemplates() {
  return useQuery({
    queryKey: ["pipeline-templates"] as const,
    queryFn: ({ signal }) => api.get<PipelineTemplate[]>("/api/pipelines/templates", signal),
    staleTime: Infinity,
  });
}

/** Creates the project, then (optionally) stores the chosen starter pipeline in both environments. */
export function useCreateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: { body: CreateProjectBody; jenkinsfile: string | null }) => {
      const project = await api.post<Project>("/api/projects", input.body);
      if (input.jenkinsfile !== null) {
        for (const env of ["development", "production"] as const) {
          await api.put<PipelineDocument>(`${envPath(project.slug, env)}/pipeline`, {
            jenkinsfile: input.jenkinsfile,
            base_version: 0,
          });
        }
      }
      return project;
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: keys.projects });
    },
  });
}

export function useUpdateProject(slug: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { name?: string; repository?: string; git_transport?: GitTransport }) =>
      api.patch<Project>(projectPath(slug), body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: keys.projects });
    },
  });
}

export function useUpdateEnvironment(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { branch?: string; auto_deploy?: boolean }) =>
      api.patch<EnvironmentSummary>(envPath(slug, env), body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: keys.projects });
    },
  });
}

// ---- retry -------------------------------------------------------------------------------

export function useRetryRun(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<RunSummary>(`/api/runs/${id}/retry`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["runs"] });
      await client.invalidateQueries({ queryKey: keys.projects });
    },
  });
}

// ---- secrets -----------------------------------------------------------------------------

const secretKeys = (slug: string, env: EnvironmentName) => ["secrets", slug, env] as const;

export function useSecrets(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: secretKeys(slug, env),
    queryFn: ({ signal }) => api.get<SecretInfo[]>(`${envPath(slug, env)}/secrets`, signal),
  });
}

function useInvalidateSecrets(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: secretKeys(slug, env) });
    await client.invalidateQueries({ queryKey: pipelineKeys.secrets(slug, env) });
  };
}

export function useSetSecret(slug: string, env: EnvironmentName) {
  const invalidate = useInvalidateSecrets(slug, env);
  return useMutation({
    mutationFn: (input: { name: string; value: string }) =>
      api.put<SecretInfo>(`${envPath(slug, env)}/secrets/${encodeURIComponent(input.name)}`, {
        value: input.value,
      }),
    onSuccess: invalidate,
  });
}

export function useDeleteSecret(slug: string, env: EnvironmentName) {
  const invalidate = useInvalidateSecrets(slug, env);
  return useMutation({
    mutationFn: (name: string) =>
      api.delete<undefined>(`${envPath(slug, env)}/secrets/${encodeURIComponent(name)}`),
    onSuccess: invalidate,
  });
}

/** Not cached: a revealed value must not linger in the query cache. */
export function useRevealSecret(slug: string, env: EnvironmentName) {
  return useMutation({
    mutationFn: (name: string) =>
      api.post<{ name: string; value: string }>(
        `${envPath(slug, env)}/secrets/${encodeURIComponent(name)}/reveal`,
      ),
    gcTime: 0,
  });
}

// ---- tokens ------------------------------------------------------------------------------

const tokenKeys = ["tokens"] as const;

export function useTokens() {
  return useQuery({
    queryKey: tokenKeys,
    queryFn: ({ signal }) => api.get<TokenInfo[]>("/api/tokens", signal),
  });
}

export function useCreateToken() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateTokenBody) => api.post<CreatedToken>("/api/tokens", body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: tokenKeys });
    },
  });
}

export function useRevokeToken() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete<TokenInfo>(`/api/tokens/${id}`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: tokenKeys });
    },
  });
}

// ---- domains -----------------------------------------------------------------------------

const domainKeys = (slug: string, env: EnvironmentName) => ["domains", slug, env] as const;
const domainPath = (slug: string, env: EnvironmentName, id: number) =>
  `${envPath(slug, env)}/domains/${id}`;

export function useDomains(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: domainKeys(slug, env),
    queryFn: ({ signal }) => api.get<DomainDetail[]>(`${envPath(slug, env)}/domains`, signal),
  });
}

function useRefreshDomains(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: domainKeys(slug, env) });
  };
}

export function useCreateDomain(slug: string, env: EnvironmentName) {
  const refresh = useRefreshDomains(slug, env);
  return useMutation({
    mutationFn: (body: CreateDomainBody) => api.post<Domain>(`${envPath(slug, env)}/domains`, body),
    onSuccess: refresh,
  });
}

export function useDeleteDomain(slug: string, env: EnvironmentName) {
  const refresh = useRefreshDomains(slug, env);
  return useMutation({
    mutationFn: (id: number) => api.delete<undefined>(domainPath(slug, env, id)),
    onSuccess: refresh,
  });
}

export function usePreviewDomain(slug: string, env: EnvironmentName) {
  return useMutation({
    mutationFn: (input: { id: number; operation: PlanOperation }) =>
      api.post<Plan>(`${domainPath(slug, env, input.id)}/preview`, { operation: input.operation }),
  });
}

/** Apply or remove: the server recomputes the plan and refuses when it differs from the hash. */
export function useExecutePlan(slug: string, env: EnvironmentName) {
  const refresh = useRefreshDomains(slug, env);
  return useMutation({
    mutationFn: (input: { id: number; operation: PlanOperation; plan_hash: string }) =>
      api.post<Plan>(`${domainPath(slug, env, input.id)}/${input.operation}`, {
        plan_hash: input.plan_hash,
      }),
    onSettled: refresh,
  });
}

export function useCheckDomain(slug: string, env: EnvironmentName) {
  const refresh = useRefreshDomains(slug, env);
  return useMutation({
    mutationFn: (id: number) => api.post<Check>(`${domainPath(slug, env, id)}/check`),
    onSuccess: refresh,
  });
}

export function useIssueCertificate(slug: string, env: EnvironmentName) {
  const refresh = useRefreshDomains(slug, env);
  return useMutation({
    mutationFn: (id: number) => api.post<Certificate>(`${domainPath(slug, env, id)}/certificate`),
    onSettled: refresh,
  });
}
