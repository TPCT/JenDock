import { setupWorker } from "msw/browser";
import { seedBulkTokens } from "./admin";
import { seedBulkContainers } from "./containers";
import { enableBulkMockData } from "./data";
import { seedBulkDeployments } from "./deployments";
import { handlers } from "./handlers";
import { seedBulkDeliveries } from "./notifications";

// The dev mock mode shows lists long enough to page; tests keep the small base data.
enableBulkMockData();
seedBulkTokens();
seedBulkContainers();
seedBulkDeliveries();
seedBulkDeployments();

export const worker = setupWorker(...handlers);
