import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  ChangePasswordBody,
  CreateUserBody,
  Grant,
  OneTimePassword,
  ProjectAccess,
  SetGrantBody,
  UpdateUserBody,
  UserSummary,
} from "./accessTypes";
import { keys } from "./hooks";
import type { Session } from "./types";

export const accessKeys = {
  users: ["users"] as const,
  userAccess: (id: number) => ["users", id, "access"] as const,
  projectAccess: (slug: string) => ["access", slug] as const,
};

const projectPath = (slug: string) => `/api/projects/${encodeURIComponent(slug)}`;

export function useUsers() {
  return useQuery({
    queryKey: accessKeys.users,
    queryFn: ({ signal }) => api.get<UserSummary[]>("/api/users", signal),
  });
}

export function useUserAccess(userId: number) {
  return useQuery({
    queryKey: accessKeys.userAccess(userId),
    queryFn: ({ signal }) => api.get<Grant[]>(`/api/users/${userId}/access`, signal),
  });
}

/** Every change to a user or a grant can change what other screens may show. */
function useRefreshAccess() {
  const client = useQueryClient();
  return async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: accessKeys.users }),
      client.invalidateQueries({ queryKey: ["access"] }),
      client.invalidateQueries({ queryKey: keys.session }),
    ]);
  };
}

export function useCreateUser() {
  const refresh = useRefreshAccess();
  return useMutation({
    mutationFn: (body: CreateUserBody) => api.post<OneTimePassword>("/api/users", body),
    onSuccess: refresh,
  });
}

export function useUpdateUser(id: number) {
  const refresh = useRefreshAccess();
  return useMutation({
    mutationFn: (body: UpdateUserBody) => api.patch<UserSummary>(`/api/users/${id}`, body),
    onSuccess: refresh,
  });
}

export function useDeleteUser() {
  const refresh = useRefreshAccess();
  return useMutation({
    mutationFn: (id: number) => api.delete<undefined>(`/api/users/${id}`),
    onSuccess: refresh,
  });
}

export function useResetPassword() {
  const refresh = useRefreshAccess();
  return useMutation({
    mutationFn: (id: number) => api.post<OneTimePassword>(`/api/users/${id}/reset-password`),
    onSuccess: refresh,
  });
}

export function useProjectAccess(slug: string) {
  return useQuery({
    queryKey: accessKeys.projectAccess(slug),
    queryFn: ({ signal }) => api.get<ProjectAccess>(`${projectPath(slug)}/access`, signal),
  });
}

export function useSetGrant(slug: string) {
  const refresh = useRefreshAccess();
  return useMutation({
    mutationFn: (body: SetGrantBody) => api.put<Grant>(`${projectPath(slug)}/access`, body),
    onSuccess: refresh,
  });
}

export function useRemoveGrant(slug: string) {
  const refresh = useRefreshAccess();
  return useMutation({
    mutationFn: (grantId: number) =>
      api.delete<undefined>(`${projectPath(slug)}/access/${grantId}`),
    onSuccess: refresh,
  });
}

/** Changes the caller's own password; the response is the refreshed session. */
export function useChangePassword() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ChangePasswordBody) => api.post<Session>("/api/auth/change-password", body),
    onSuccess: (session) => {
      client.setQueryData(keys.session, session);
    },
  });
}
