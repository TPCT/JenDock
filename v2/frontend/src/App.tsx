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
import { NotFoundPage, ProjectTabPlaceholder, SettingsPlaceholderPage } from "./pages/Placeholder";
import { ProjectLayout } from "./pages/ProjectLayout";
import { ProjectOverviewPage } from "./pages/ProjectOverview";
import { ProjectsPage } from "./pages/Projects";
import { RunDetailPage } from "./pages/RunDetail";
import { AllRunsPage, EnvironmentRunsPage } from "./pages/Runs";

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

function RequireAuth({ children }: { children: ReactNode }) {
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
  return children;
}

const PipelinePage = lazy(() => import("./pages/Pipeline"));

function Root() {
  return (
    <>
      <UnauthenticatedRedirect />
      <Outlet />
    </>
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
        element: (
          <RequireAuth>
            <Shell />
          </RequireAuth>
        ),
        children: [
          { index: true, element: <Navigate to="/projects" replace /> },
          { path: "/projects", element: <ProjectsPage /> },
          {
            path: "/projects/:slug",
            element: <ProjectLayout />,
            children: [
              { index: true, element: <ProjectOverviewPage /> },
              { path: "environments/:env/runs", element: <EnvironmentRunsPage /> },
              { path: "environments/:env/pipeline", element: <PipelineRoute /> },
              { path: "containers", element: <ProjectTabPlaceholder /> },
              { path: "database", element: <ProjectTabPlaceholder /> },
              { path: "domains", element: <ProjectTabPlaceholder /> },
              { path: "settings", element: <ProjectTabPlaceholder /> },
            ],
          },
          { path: "/runs", element: <AllRunsPage /> },
          { path: "/runs/:id", element: <RunDetailPage /> },
          { path: "/settings", element: <SettingsPlaceholderPage /> },
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
