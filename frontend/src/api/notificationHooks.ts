import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { usePagedQuery } from "./pagedHooks";
import type {
  CreateChannelBody,
  Delivery,
  NotificationChannel,
  NotificationPolicy,
  RuleItem,
  RuleSet,
  TestResult,
  UpdateChannelBody,
} from "./notificationTypes";
import type { EnvironmentName } from "./types";

const notificationKeys = {
  all: ["notifications"] as const,
  policy: ["notifications", "policy"] as const,
  channels: ["notifications", "channels"] as const,
  rules: (slug: string | null, env: EnvironmentName | null) =>
    ["notifications", "rules", slug, env] as const,
  deliveries: (slug: string | null) => ["notifications", "deliveries", slug] as const,
};

const rulesPath = (slug: string | null, env: EnvironmentName | null): string => {
  if (slug === null) return "/api/notifications/rules";
  const project = `/api/projects/${encodeURIComponent(slug)}`;
  return env === null
    ? `${project}/notifications/rules`
    : `${project}/environments/${env}/notifications/rules`;
};

export function useNotificationPolicy() {
  return useQuery({
    queryKey: notificationKeys.policy,
    queryFn: ({ signal }) => api.get<NotificationPolicy>("/api/notifications/policy", signal),
    staleTime: Infinity,
  });
}

export function useChannels() {
  return useQuery({
    queryKey: notificationKeys.channels,
    queryFn: ({ signal }) => api.get<NotificationChannel[]>("/api/notifications/channels", signal),
  });
}

function useRefreshChannels() {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: notificationKeys.channels });
    await client.invalidateQueries({ queryKey: ["notifications", "deliveries"] });
    await client.invalidateQueries({ queryKey: ["notifications", "rules"] });
  };
}

export function useCreateChannel() {
  const refresh = useRefreshChannels();
  return useMutation({
    mutationFn: (body: CreateChannelBody) =>
      api.post<NotificationChannel>("/api/notifications/channels", body),
    onSuccess: refresh,
  });
}

export function useUpdateChannel(id: number) {
  const refresh = useRefreshChannels();
  return useMutation({
    mutationFn: (body: UpdateChannelBody) =>
      api.patch<NotificationChannel>(`/api/notifications/channels/${id}`, body),
    onSuccess: refresh,
  });
}

export function useDeleteChannel() {
  const refresh = useRefreshChannels();
  return useMutation({
    mutationFn: (id: number) => api.delete<undefined>(`/api/notifications/channels/${id}`),
    onSuccess: refresh,
  });
}

/** Sends a real test message; a failed delivery is a normal result (`ok: false`). */
export function useTestChannel() {
  const refresh = useRefreshChannels();
  return useMutation({
    mutationFn: (id: number) => api.post<TestResult>(`/api/notifications/channels/${id}/test`),
    onSettled: refresh,
  });
}

export function useRules(slug: string | null, env: EnvironmentName | null) {
  return useQuery({
    queryKey: notificationKeys.rules(slug, env),
    queryFn: ({ signal }) => api.get<RuleSet>(rulesPath(slug, env), signal),
  });
}

export function useSaveRules(slug: string | null, env: EnvironmentName | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (rules: RuleItem[]) => api.put<RuleSet>(rulesPath(slug, env), { rules }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["notifications", "rules"] });
    },
  });
}

export function useDeliveries(slug: string | null, apiQuery: string) {
  const params = new URLSearchParams(apiQuery);
  if (slug !== null) params.set("project", slug);
  return usePagedQuery<Delivery>(
    notificationKeys.deliveries(slug),
    "/api/notifications/deliveries",
    params.toString(),
    { refetchInterval: 10_000 },
  );
}
