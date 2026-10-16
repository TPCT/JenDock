import { useSession } from "../api/hooks";
import type { EnvironmentName, Permissions } from "../api/types";

/** Policy actions the UI asks about (the server names them the same way). */
export type Action =
  | "project.read"
  | "project.create"
  | "project.update"
  | "environment.update"
  | "environment.power_read"
  | "environment.start"
  | "environment.stop"
  | "run.read"
  | "run.create"
  | "run.cancel"
  | "run.retry"
  | "pipeline.read"
  | "pipeline.write"
  | "deployment.read"
  | "deployment.redeploy"
  | "container.read"
  | "container.logs"
  | "container.terminal"
  | "container.action"
  | "container.env_update"
  | "container.limits"
  | "container.files"
  | "container.env_reveal"
  | "database.read"
  | "database.data"
  | "database.configure"
  | "database.detect"
  | "domain.read"
  | "domain.manage"
  | "domain.discover"
  | "domain.discover_refresh"
  | "domain.lookup"
  | "domain.adopt"
  | "domain.adopt_all"
  | "secret.list"
  | "secret.write"
  | "secret.reveal"
  | "user.manage"
  | "access.manage"
  | "integration.manage"
  | "notification.manage";

export interface Scope {
  project?: string | undefined;
  env?: EnvironmentName | undefined;
  /** The compose service of a container (container-level grants). */
  service?: string | null | undefined;
}

/** Whether the permission summary allows `action` (without a project: a platform-wide action). */
export function allows(permissions: Permissions | undefined, action: Action, scope?: Scope) {
  if (!permissions) return false;
  if (!scope?.project) return permissions.global.includes(action);
  if (permissions.all) return true;
  const environments = permissions.projects[scope.project]?.environments;
  if (!environments) return false;
  const names = scope.env ? [scope.env] : (Object.keys(environments) as EnvironmentName[]);
  return names.some((name) => {
    const environment = environments[name];
    if (!environment) return false;
    if (environment.actions.includes(action)) return true;
    return scope.service ? (environment.services[scope.service]?.includes(action) ?? false) : false;
  });
}

/** `can(action, scope)` for the signed-in user; false until the session is known. */
export function useCan(): (action: Action, scope?: Scope) => boolean {
  const permissions = useSession().data?.permissions;
  return (action, scope) => allows(permissions, action, scope);
}
