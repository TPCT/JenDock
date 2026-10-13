/** Wire types of the deployment history. Keep in sync with docs/api-contract.md. */
import type { RunSummary } from "./types";

export type DeploymentStatus =
  "pending" | "deploying" | "succeeded" | "failed" | "rolled_back" | "rollback_failed";

export interface ServiceImage {
  service: string;
  previous_image: string | null;
  previous_image_id: string | null;
  new_image: string;
  new_image_id: string | null;
}

export interface Deployment {
  id: number;
  run_id: number;
  run_number: number;
  status: DeploymentStatus;
  commit_sha: string;
  services: ServiceImage[];
  message: string | null;
  /** The earlier deployment whose images this one reuses (a redeploy). */
  redeploy_of: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RedeployResult {
  run: RunSummary;
  deployment_id: number;
}
