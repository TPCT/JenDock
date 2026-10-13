/** Wire types of members and access grants. Keep in sync with docs/api-contract.md. */
import type { AccessRole, ContainerPermission, EnvironmentName, GlobalRole } from "./types";

export const GLOBAL_ROLES: readonly GlobalRole[] = ["owner", "admin", "member"];
export const ACCESS_ROLES: readonly AccessRole[] = ["viewer", "developer", "maintainer"];
export const CONTAINER_PERMISSIONS: readonly ContainerPermission[] = [
  "logs",
  "terminal",
  "actions",
];

export interface UserSummary {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  role: GlobalRole;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
}

export interface CreateUserBody {
  username: string;
  display_name?: string;
  email?: string | null;
  role: GlobalRole;
}

export interface UpdateUserBody {
  display_name?: string;
  email?: string | null;
  role?: GlobalRole;
  is_active?: boolean;
}

/** A user together with a generated password that is shown exactly once. */
export interface OneTimePassword {
  user: UserSummary;
  one_time_password: string;
}

export interface Grant {
  id: number;
  user_id: number;
  username: string;
  display_name: string;
  project: string;
  /** `null`: the whole project. */
  environment: EnvironmentName | null;
  /** Set: container permissions on this compose service of `environment`. */
  service: string | null;
  role: AccessRole | null;
  permissions: ContainerPermission[];
  created_at: string;
}

export interface ProjectAccess {
  /** Owners and admins: full access without a grant. */
  privileged: UserSummary[];
  grants: Grant[];
}

export interface SetGrantBody {
  user_id: number;
  environment?: EnvironmentName;
  service?: string;
  role?: AccessRole;
  permissions?: ContainerPermission[];
}

export interface ChangePasswordBody {
  current_password: string;
  new_password: string;
}
