import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  AccountSession,
  AuditFilters,
  AuditPage,
  Dashboard,
  Profile,
  RecoveryCodes,
  TwoFactorEnrollment,
  TwoFactorStatus,
  UpdateProfileBody,
} from "./accountTypes";

export const accountKeys = {
  profile: ["me"] as const,
  sessions: ["me", "sessions"] as const,
  twoFactor: ["me", "two-factor"] as const,
  securityLog: ["me", "security-log"] as const,
  audit: (filters: AuditFilters) => ["audit", filters] as const,
  auditActions: ["audit", "actions"] as const,
  dashboard: ["dashboard"] as const,
};

// ---- profile -------------------------------------------------------------------------------

export function useProfile() {
  return useQuery({
    queryKey: accountKeys.profile,
    queryFn: ({ signal }) => api.get<Profile>("/api/me", signal),
    staleTime: 60_000,
  });
}

export function useUpdateProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: UpdateProfileBody) => api.patch<Profile>("/api/me", body),
    onSuccess: (profile) => {
      client.setQueryData(accountKeys.profile, profile);
      void client.invalidateQueries({ queryKey: ["session"] });
      void client.invalidateQueries({ queryKey: accountKeys.securityLog });
    },
  });
}

// ---- sessions ------------------------------------------------------------------------------

export function useAccountSessions() {
  return useQuery({
    queryKey: accountKeys.sessions,
    queryFn: ({ signal }) => api.get<AccountSession[]>("/api/me/sessions", signal),
  });
}

export function useRevokeSession() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete<undefined>(`/api/me/sessions/${String(id)}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: accountKeys.sessions });
    },
  });
}

export function useRevokeOtherSessions() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ sessions_ended: number }>("/api/me/sessions/revoke-others"),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: accountKeys.sessions });
    },
  });
}

// ---- two-factor ----------------------------------------------------------------------------

export function useTwoFactorStatus() {
  return useQuery({
    queryKey: accountKeys.twoFactor,
    queryFn: ({ signal }) => api.get<TwoFactorStatus>("/api/me/two-factor", signal),
  });
}

function useTwoFactorMutation<TInput, TResult>(fn: (input: TInput) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: accountKeys.twoFactor });
      void client.invalidateQueries({ queryKey: accountKeys.profile });
      void client.invalidateQueries({ queryKey: accountKeys.securityLog });
    },
  });
}

export function useBeginTwoFactor() {
  return useMutation({
    mutationFn: () => api.post<TwoFactorEnrollment>("/api/me/two-factor/enroll"),
  });
}

export function useEnableTwoFactor() {
  return useTwoFactorMutation((code: string) =>
    api.post<RecoveryCodes>("/api/me/two-factor/enable", { code }),
  );
}

export function useRegenerateRecoveryCodes() {
  return useTwoFactorMutation((code: string) =>
    api.post<RecoveryCodes>("/api/me/two-factor/recovery-codes", { code }),
  );
}

export function useDisableTwoFactor() {
  return useTwoFactorMutation((input: { password: string; code: string }) =>
    api.post<undefined>("/api/me/two-factor/disable", input),
  );
}

// ---- activity ------------------------------------------------------------------------------

export function useSecurityLog() {
  return useInfiniteQuery({
    queryKey: accountKeys.securityLog,
    queryFn: ({ pageParam, signal }) =>
      api.get<AuditPage>(
        `/api/me/security-log?limit=25${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`,
        signal,
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** The query string of the audit filters; empty values are left out. */
export function auditQuery(filters: AuditFilters): string {
  const params = new URLSearchParams();
  for (const key of Object.keys(filters) as (keyof AuditFilters)[]) {
    const value = filters[key];
    if (value === "") continue;
    // The date inputs hold local `YYYY-MM-DD`; the API wants an aware timestamp.
    if (key === "since") params.set(key, new Date(`${value}T00:00:00`).toISOString());
    else if (key === "until") {
      const end = new Date(`${value}T00:00:00`);
      end.setDate(end.getDate() + 1);
      params.set(key, end.toISOString());
    } else params.set(key, value);
  }
  return params.toString();
}

export function useAuditLog(filters: AuditFilters) {
  return useInfiniteQuery({
    queryKey: accountKeys.audit(filters),
    queryFn: ({ pageParam, signal }) => {
      const query = new URLSearchParams(auditQuery(filters));
      query.set("limit", "50");
      if (pageParam) query.set("cursor", pageParam);
      return api.get<AuditPage>(`/api/audit?${query.toString()}`, signal);
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useAuditActions() {
  return useQuery({
    queryKey: accountKeys.auditActions,
    queryFn: ({ signal }) => api.get<{ actions: string[] }>("/api/audit/actions", signal),
    staleTime: Infinity,
  });
}

// ---- dashboard -----------------------------------------------------------------------------

export function useDashboard() {
  return useQuery({
    queryKey: accountKeys.dashboard,
    queryFn: ({ signal }) => api.get<Dashboard>("/api/dashboard", signal),
    refetchInterval: 15_000,
  });
}
