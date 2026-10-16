import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import {
  Navigate,
  Outlet,
  RouterProvider,
  createBrowserRouter,
  useLocation,
  useNavigate,
  type RouteObject,
} from "react-router-dom";
import { isApiError, onUnauthenticated } from "./api/client";
import { useSession } from "./api/hooks";
import { BaseStyles } from "@primer/react";
import styles from "./App.module.css";
import { ErrorCallout, Loading } from "./components/Feedback";
import { Shell } from "./components/Shell";
import { LoginPage } from "./pages/Login";
import { ContainerDetailPage } from "./pages/ContainerDetail";
import { ContainersPage } from "./pages/Containers";
import { DatabaseBrowserPage } from "./pages/DatabaseBrowser";
import { DatabasesPage } from "./pages/Databases";
import { DeploymentsPage } from "./pages/Deployments";
import { DiscoveredDomainsPage } from "./pages/DiscoveredDomains";
import { DomainsPage } from "./pages/Domains";
import { ChangePasswordPage } from "./pages/ChangePassword";
import { MembersPage } from "./pages/Members";
import { AccessSettingsPage } from "./pages/ProjectAccess";
import { AgentsPage } from "./pages/AgentsPage";
import { GlobalSettingsLayout, TokensPage } from "./pages/GlobalSettings";
import { IntegrationsPage } from "./pages/Integrations";
import { NotificationsPage } from "./pages/Notifications";
import { ProfilePage, SecurityLogPage, SecurityPage, SessionsPage } from "./pages/Account";
import { AuditLogPage } from "./pages/AuditLog";
import { HomePage } from "./pages/Home";
import { NewProjectPage } from "./pages/NewProject";
import { NotFoundPage } from "./pages/Placeholder";
import {
  EnvironmentsSettingsPage,
  GeneralSettingsPage,
  ProjectSettingsLayout,
  WebhookSettingsPage,
} from "./pages/ProjectSettings";
import { ProjectNotificationsPage } from "./pages/ProjectNotifications";
import { SecretsSettingsPage } from "./pages/SecretsSettings";
import { ProjectLayout } from "./pages/ProjectLayout";
import { ProjectOverviewPage } from "./pages/ProjectOverview";
import { ProjectsPage } from "./pages/Projects";
import { RunDetailPage } from "./pages/RunDetail";
import { AllRunsPage, EnvironmentRunsPage } from "./pages/Runs";
import { BackupsPage } from "./pages/Backups";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (count, error) =>
          !(isApiError(error) && error.status < 500 && error.status !== 0) && count < 2,
        refetchOnWindowFocus: false,
        staleTime: 5_000,
      },
    },
  });
}

/** Any 401 sends the user to the login page and returns them to where they were afterwards. */
export function UnauthenticatedRedirect() {
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(() => {
    onUnauthenticated(() => {
      if (location.pathname.startsWith("/login")) return;
      const next = encodeURIComponent(location.pathname + location.search);
      void navigate(`/login?next=${next}`, { replace: true });
    });
    return () => {
      onUnauthenticated(null);
    };
  }, [navigate, location.pathname, location.search]);
  return null;
}

/**
 * Signed-in users only. After an administrator reset the password the session is limited to the
 * change-password screen (`pending` marks the one route that is open to it).
 */
function RequireAuth({ children, pending = false }: { children: ReactNode; pending?: boolean }) {
  const session = useSession();
  const location = useLocation();
  if (session.isPending) return <Loading label="Checking your session" />;
  if (session.isError) {
    if (isApiError(session.error) && session.error.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search);
      return <Navigate to={`/login?next=${next}`} replace />;
    }
    return (
      <div className={styles.fullPage}>
        <ErrorCallout
          title="Could not reach the server"
          error={session.error}
          onRetry={() => void session.refetch()}
        />
      </div>
    );
  }
  if (session.data.must_change_password && !pending) {
    return <Navigate to="/change-password" replace />;
  }
  return children;
}

const PipelinePage = lazy(() => import("./pages/Pipeline"));
const TerminalPage = lazy(() => import("./pages/TerminalPage"));
const ConsolePage = lazy(() => import("./pages/DatabaseConsole"));

function Root() {
  return (
    <>
      <UnauthenticatedRedirect />
      <Outlet />
    </>
  );
}

function TerminalRoute() {
  return (
    <Suspense fallback={<Loading label="Loading the terminal" />}>
      <TerminalPage />
    </Suspense>
  );
}

function ConsoleRoute() {
  return (
    <Suspense fallback={<Loading label="Loading the SQL console" />}>
      <ConsolePage />
    </Suspense>
  );
}

function PipelineRoute() {
  return (
    <Suspense fallback={<Loading label="Loading the pipeline editor" />}>
      <PipelinePage />
    </Suspense>
  );
}

export const appRoutes: RouteObject[] = [
  {
    element: <Root />,
    children: [
      { path: "/login", element: <LoginPage /> },
      {
        path: "/change-password",
        element: (
          <RequireAuth pending>
            <ChangePasswordPage />
          </RequireAuth>
        ),
      },
      {
        element: (
          <RequireAuth>
            <Shell />
          </RequireAuth>
        ),
        children: [
          { index: true, element: <HomePage /> },
          { path: "/audit", element: <AuditLogPage /> },
          { path: "/domains", element: <DiscoveredDomainsPage /> },
          { path: "/projects", element: <ProjectsPage /> },
          { path: "/projects/new", element: <NewProjectPage /> },
          {
            path: "/projects/:slug",
            element: <ProjectLayout />,
            children: [
              { index: true, element: <ProjectOverviewPage /> },
              { path: "environments/:env/runs", element: <EnvironmentRunsPage /> },
              { path: "environments/:env/pipeline", element: <PipelineRoute /> },
              { path: "environments/:env/deployments", element: <DeploymentsPage /> },
              { path: "environments/:env/containers", element: <ContainersPage /> },
              { path: "environments/:env/containers/:id", element: <ContainerDetailPage /> },
              { path: "environments/:env/containers/:id/terminal", element: <TerminalRoute /> },
              {
                path: "containers",
                element: <Navigate to="../environments/development/containers" replace />,
              },
              { path: "environments/:env/databases", element: <DatabasesPage /> },
              { path: "environments/:env/databases/:name", element: <DatabaseBrowserPage /> },
              { path: "environments/:env/databases/:name/console", element: <ConsoleRoute /> },
              {
                path: "database",
                element: <Navigate to="../environments/development/databases" replace />,
              },
              { path: "environments/:env/domains", element: <DomainsPage /> },
              {
                path: "domains",
                element: <Navigate to="../environments/development/domains" replace />,
              },
              {
                path: "settings",
                element: <ProjectSettingsLayout />,
                children: [
                  { index: true, element: <GeneralSettingsPage /> },
                  { path: "environments", element: <EnvironmentsSettingsPage /> },
                  { path: "webhook", element: <WebhookSettingsPage /> },
                  { path: "secrets", element: <SecretsSettingsPage /> },
                  { path: "access", element: <AccessSettingsPage /> },
                  { path: "notifications", element: <ProjectNotificationsPage /> },
                  { path: "*", element: <NotFoundPage /> },
                ],
              },
              { path: "*", element: <NotFoundPage /> },
            ],
          },
          { path: "/runs", element: <AllRunsPage /> },
          { path: "/backups", element: <BackupsPage /> },
          { path: "/runs/:id", element: <RunDetailPage /> },
          {
            path: "/settings",
            element: <GlobalSettingsLayout />,
            children: [
              { index: true, element: <Navigate to="/settings/profile" replace /> },
              { path: "profile", element: <ProfilePage /> },
              { path: "sessions", element: <SessionsPage /> },
              { path: "security-log", element: <SecurityLogPage /> },
              { path: "tokens", element: <TokensPage /> },
              { path: "notifications", element: <NotificationsPage /> },
              { path: "agents", element: <AgentsPage /> },
              { path: "account", element: <SecurityPage /> },
              { path: "members", element: <MembersPage /> },
              { path: "integrations", element: <IntegrationsPage /> },
              { path: "*", element: <NotFoundPage /> },
            ],
          },
          { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
];

export function App() {
  const [client] = useState(createQueryClient);
  const [router] = useState(() => createBrowserRouter(appRoutes));
  return (
    <QueryClientProvider client={client}>
      <BaseStyles>
        <RouterProvider router={router} />
      </BaseStyles>
    </QueryClientProvider>
  );
}
