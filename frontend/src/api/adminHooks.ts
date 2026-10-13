import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { usePagedQuery } from "./pagedHooks";
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

export interface WizardDomain {
  env: EnvironmentName;
  body: CreateDomainBody;
}

export interface CreateProjectResult {
  project: Project;
  /** Domains saved as drafts, in the order given. */
  created: WizardDomain[];
  /** Domains the server refused; the project exists anyway. */
  failed: { domain: WizardDomain; message: string }[];
}

/**
 * Creates the project, then (optionally) stores the chosen starter pipeline in both environments
 * and saves each chosen domain as a draft. A domain that is refused does not undo the project:
 * it comes back in `failed` so the next page can say so.
 */
export function useCreateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      body: CreateProjectBody;
      jenkinsfile: string | null;
      domains?: WizardDomain[];
    }): Promise<CreateProjectResult> => {
      const project = await api.post<Project>("/api/projects", input.body);
      if (input.jenkinsfile !== null) {
        for (const env of ["development", "production"] as const) {
          await api.put<PipelineDocument>(`${envPath(project.slug, env)}/pipeline`, {
            jenkinsfile: input.jenkinsfile,
            base_version: 0,
          });
        }
      }
      const result: CreateProjectResult = { project, created: [], failed: [] };
      for (const domain of input.domains ?? []) {
        try {
          await api.post<Domain>(`${envPath(project.slug, domain.env)}/domains`, domain.body);
          result.created.push(domain);
        } catch (error) {
          const message = error instanceof Error ? error.message : "the request failed";
          result.failed.push({ domain, message });
        }
      }
      return result;
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

export interface AutoDeployTarget {
  slug: string;
  env: EnvironmentName;
}

export interface BulkAutoDeployResult {
  enabled: AutoDeployTarget[];
  failed: { target: AutoDeployTarget; message: string }[];
}

/** One PATCH per environment (each audited on its own); a failure does not stop the rest. */
export function useEnableAutoDeploy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (targets: AutoDeployTarget[]): Promise<BulkAutoDeployResult> => {
      const result: BulkAutoDeployResult = { enabled: [], failed: [] };
      for (const target of targets) {
        try {
          await api.patch<EnvironmentSummary>(envPath(target.slug, target.env), {
            auto_deploy: true,
          });
          result.enabled.push(target);
        } catch (error) {
          const message = error instanceof Error ? error.message : "request failed";
          result.failed.push({ target, message });
        }
      }
      return result;
    },
    onSettled: async () => {
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

export function useTokens(apiQuery: string) {
  return usePagedQuery<TokenInfo>(tokenKeys, "/api/tokens", apiQuery);
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
